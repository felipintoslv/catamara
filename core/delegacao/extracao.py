#!/usr/bin/env python3
"""Extracao estruturada e consolidacao de dados tabulares sem criacao de fatos."""

from __future__ import annotations

import argparse
import datetime
import json
import re
import sys
from typing import Any

NOME_CAMPO_RE = re.compile(r"^[a-z_][a-z0-9_]{0,40}$")
INT_STR_RE = re.compile(r"^-?\d+$")
NUMERO_STR_RE = re.compile(r"^-?\d+([.,]\d+)?$")
DATA_ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
DATA_BR_RE = re.compile(r"^(\d{2})/(\d{2})/(\d{4})$")

TIPOS_ACEITOS = {"int", "numero", "texto", "bool", "data", "lista_texto"}

AGREGACOES_CAMPOS = {
    "soma": {"int", "numero"},
    "media": {"int", "numero"},
    "min": {"int", "numero", "data"},
    "max": {"int", "numero", "data"},
    "unicos": {"int", "numero", "texto", "bool", "data", "lista_texto"},
}


def validar_esquema(declaracoes: list[str]) -> dict[str, str]:
    """Valida as declaracoes do esquema e retorna mapeamento nome -> tipo."""
    if not declaracoes:
        raise ValueError("esquema vazio")
    campos: dict[str, str] = {}
    for decl in declaracoes:
        if ":" not in decl:
            raise ValueError(f"declaracao invalida (esperado nome:tipo): {decl}")
        partes = decl.split(":", 1)
        nome = partes[0].strip()
        tipo = partes[1].strip()
        if not NOME_CAMPO_RE.match(nome):
            raise ValueError(f"nome de campo invalido: {nome!r}")
        if tipo not in TIPOS_ACEITOS:
            raise ValueError(f"tipo desconhecido: {tipo!r} no campo {nome}")
        if nome in campos:
            raise ValueError(f"campo duplicado no esquema: {nome}")
        campos[nome] = tipo
    return campos


def esquema_para_json(campos: dict[str, str]) -> dict[str, Any]:
    """Gera o schema JSON para o parametro format do Ollama."""
    props: dict[str, Any] = {}
    for nome, tipo in campos.items():
        if tipo == "int":
            props[nome] = {"type": ["integer", "null"]}
        elif tipo == "numero":
            props[nome] = {"type": ["number", "null"]}
        elif tipo == "texto":
            props[nome] = {"type": ["string", "null"]}
        elif tipo == "bool":
            props[nome] = {"type": ["boolean", "null"]}
        elif tipo == "data":
            props[nome] = {
                "type": ["string", "null"],
                "pattern": r"^(\d{4}-\d{2}-\d{2}|\d{2}/\d{2}/\d{4})$",
            }
        elif tipo == "lista_texto":
            props[nome] = {
                "type": ["array", "null"],
                "items": {"type": "string"},
            }

    return {
        "type": "object",
        "properties": {
            "registros": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": props,
                    "required": list(campos.keys()),
                    "additionalProperties": False,
                },
            }
        },
        "required": ["registros"],
        "additionalProperties": False,
    }


def _validar_data(s: str) -> str:
    s = s.strip()
    if DATA_ISO_RE.match(s):
        ano, mes, dia = int(s[0:4]), int(s[5:7]), int(s[8:10])
        datetime.date(ano, mes, dia)
        return s
    m = DATA_BR_RE.match(s)
    if m:
        dia, mes, ano = int(m.group(1)), int(m.group(2)), int(m.group(3))
        datetime.date(ano, mes, dia)
        return f"{ano:04d}-{mes:02d}-{dia:02d}"
    raise ValueError(f"formato de data invalido: {s!r}")


def coagir_valor(tipo: str, valor: Any) -> Any:
    """Coage deterministamente um valor para o tipo especificado ou retorna None."""
    if valor is None:
        return None

    if tipo == "int":
        if isinstance(valor, bool):
            raise ValueError(f"booleano nao eh int valido: {valor!r}")
        if isinstance(valor, int):
            return valor
        if isinstance(valor, str):
            s = valor.strip()
            if INT_STR_RE.match(s):
                return int(s)
        raise ValueError(f"valor invalido para int: {valor!r}")

    if tipo == "numero":
        if isinstance(valor, bool):
            raise ValueError(f"booleano nao eh numero valido: {valor!r}")
        if isinstance(valor, (int, float)):
            return float(valor)
        if isinstance(valor, str):
            s = valor.strip()
            if NUMERO_STR_RE.match(s):
                return float(s.replace(",", "."))
        raise ValueError(f"valor invalido para numero: {valor!r}")

    if tipo == "texto":
        if isinstance(valor, str):
            s = valor.strip()
            return s if s else None
        if isinstance(valor, (int, float)):
            s = str(valor).strip()
            return s if s else None
        raise ValueError(f"valor invalido para texto: {valor!r}")

    if tipo == "bool":
        if isinstance(valor, bool):
            return valor
        if isinstance(valor, str):
            s = valor.strip().lower()
            if s in {"true", "sim", "1"}:
                return True
            if s in {"false", "não", "nao", "0"}:
                return False
        raise ValueError(f"valor invalido para bool: {valor!r}")

    if tipo == "data":
        if isinstance(valor, str):
            return _validar_data(valor)
        raise ValueError(f"valor invalido para data: {valor!r}")

    if tipo == "lista_texto":
        if isinstance(valor, list):
            res: list[str] = []
            for item in valor:
                if isinstance(item, str):
                    res.append(item.strip())
                elif item is not None:
                    res.append(str(item).strip())
            return res
        if isinstance(valor, str):
            s = valor.strip()
            return [s] if s else []
        raise ValueError(f"valor invalido para lista_texto: {valor!r}")

    raise ValueError(f"tipo nao suportado: {tipo!r}")


def coagir_registro(registro: Any, campos: dict[str, str]) -> dict[str, Any]:
    """Coage um registro de dicionario contra o esquema."""
    if not isinstance(registro, dict):
        raise ValueError(f"registro nao eh objeto: {type(registro).__name__}")
    saida: dict[str, Any] = {}
    for nome, tipo in campos.items():
        val = registro.get(nome)
        saida[nome] = coagir_valor(tipo, val)
    return saida


def validar_agregacoes(agregacoes: list[str], campos: dict[str, str]) -> list[tuple[str, str, str | None]]:
    """Valida especificacoes de agregacao e retorna lista (chave, op, campo)."""
    resultado: list[tuple[str, str, str | None]] = []
    for agr in agregacoes:
        agr = agr.strip()
        if not agr:
            continue
        if agr == "contagem":
            resultado.append(("contagem", "contagem", None))
            continue
        if ":" not in agr:
            raise ValueError(f"agregacao invalida (esperado op:campo): {agr}")
        op, campo = agr.split(":", 1)
        op = op.strip()
        campo = campo.strip()
        if op not in AGREGACOES_CAMPOS:
            raise ValueError(f"operacao de agregacao desconhecida: {op}")
        if campo not in campos:
            raise ValueError(f"campo de agregacao inexiste no esquema: {campo}")
        tipo_campo = campos[campo]
        if tipo_campo not in AGREGACOES_CAMPOS[op]:
            raise ValueError(
                f"agregacao {op!r} nao suporta campo {campo!r} do tipo {tipo_campo!r}"
            )
        resultado.append((f"{op}:{campo}", op, campo))
    return resultado


def calcular_agregacoes(
    registros: list[dict[str, Any]],
    specs: list[tuple[str, str, str | None]],
) -> dict[str, Any]:
    """Calcula as agregacoes sobre os registros validos consolidados."""
    saida: dict[str, Any] = {}
    for chave, op, campo in specs:
        if op == "contagem":
            saida[chave] = len(registros)
            continue

        assert campo is not None
        valores = [r[campo] for r in registros if r.get(campo) is not None]

        if op == "soma":
            if not valores:
                saida[chave] = 0.0
            else:
                saida[chave] = sum(valores)
        elif op == "media":
            if not valores:
                saida[chave] = None
            else:
                saida[chave] = sum(valores) / len(valores)
        elif op == "min":
            if not valores:
                saida[chave] = None
            else:
                saida[chave] = min(valores)
        elif op == "max":
            if not valores:
                saida[chave] = None
            else:
                saida[chave] = max(valores)
        elif op == "unicos":
            unicos_set: set[Any] = set()
            for v in valores:
                if isinstance(v, list):
                    for sub in v:
                        unicos_set.add(sub)
                else:
                    unicos_set.add(v)
            try:
                saida[chave] = sorted(list(unicos_set))
            except TypeError:
                saida[chave] = sorted([str(x) for x in unicos_set])

    return saida


def consolidar_arquivos(
    caminhos: list[str],
    declaracoes_esquema: list[str],
    declaracoes_agregacoes: list[str] | None = None,
) -> dict[str, Any]:
    """Consolida registros de multiplos arquivos JSON."""
    campos = validar_esquema(declaracoes_esquema)
    specs_agr = validar_agregacoes(declaracoes_agregacoes or [], campos)

    registros_consolidados: list[dict[str, Any]] = []
    vistos_canonicos: set[str] = set()
    erros: list[dict[str, Any]] = []
    fontes_lidas = 0

    for caminho in caminhos:
        try:
            with open(caminho, "r", encoding="utf-8") as arq:
                dados = json.load(arq)
        except Exception as exc:
            raise RuntimeError(f"erro ao ler arquivo JSON {caminho}: {exc}") from exc

        if not isinstance(dados, dict) or "registros" not in dados or not isinstance(dados["registros"], list):
            raise RuntimeError(f"arquivo {caminho} nao contem lista 'registros'")

        fontes_lidas += 1
        for idx, item in enumerate(dados["registros"]):
            try:
                coagido = coagir_registro(item, campos)
                chave = json.dumps(coagido, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
                if chave not in vistos_canonicos:
                    vistos_canonicos.add(chave)
                    registros_consolidados.append(coagido)
            except Exception as erro_reg:
                erros.append({
                    "fonte": caminho,
                    "indice": idx,
                    "motivo": str(erro_reg),
                })

    agregacoes_calc = calcular_agregacoes(registros_consolidados, specs_agr)

    return {
        "registros": registros_consolidados,
        "agregacoes": agregacoes_calc,
        "erros": erros,
        "fontes": fontes_lidas,
    }


def main(argv: list[str] | None = None) -> int:
    args_list = sys.argv[1:] if argv is None else argv
    if not args_list:
        print("uso: extracao.py [validar-esquema | schema-json | consolidar] ...", file=sys.stderr)
        return 2

    subcmd = args_list[0]
    restante = args_list[1:]

    if subcmd == "validar-esquema":
        if not restante:
            print("erro: nenhum esquema fornecido", file=sys.stderr)
            return 1
        try:
            validar_esquema(restante)
            return 0
        except Exception as exc:
            print(f"esquema invalido: {exc}", file=sys.stderr)
            return 1

    if subcmd == "schema-json":
        if not restante:
            print("erro: nenhum esquema fornecido", file=sys.stderr)
            return 2
        try:
            campos = validar_esquema(restante)
            schema = esquema_para_json(campos)
            print(json.dumps(schema, ensure_ascii=False))
            return 0
        except Exception as exc:
            print(f"esquema invalido: {exc}", file=sys.stderr)
            return 2

    if subcmd == "consolidar":
        parser = argparse.ArgumentParser(prog="extracao.py consolidar")
        parser.add_argument("--esquema", action="append", default=[], required=True)
        parser.add_argument("--agregacoes", action="append", default=[])
        parser.add_argument("arquivos", nargs="+")

        try:
            # Achatar argumentos passados com espacos
            parsed, desconhecidos = parser.parse_known_args(restante)
            if desconhecidos:
                print(f"argumentos desconhecidos: {desconhecidos}", file=sys.stderr)
                return 2

            esquema_plano: list[str] = []
            for item in parsed.esquema:
                esquema_plano.extend(item.split())

            agregacoes_plano: list[str] = []
            for item in parsed.agregacoes:
                agregacoes_plano.extend(item.split())

            # Validar esquema e agregacoes primeiro (codigo 2 se invalidos)
            campos = validar_esquema(esquema_plano)
            validar_agregacoes(agregacoes_plano, campos)
        except ValueError as err_val:
            print(f"erro nos argumentos: {err_val}", file=sys.stderr)
            return 2
        except Exception as err_args:
            print(f"erro ao processar opcoes: {err_args}", file=sys.stderr)
            return 2

        try:
            resultado = consolidar_arquivos(parsed.arquivos, esquema_plano, agregacoes_plano)
            print(json.dumps(resultado, ensure_ascii=False))
            return 0
        except RuntimeError as err_run:
            print(f"falha de leitura: {err_run}", file=sys.stderr)
            return 1
        except Exception as err_gen:
            print(f"falha na consolidacao: {err_gen}", file=sys.stderr)
            return 1

    print(f"subcomando desconhecido: {subcmd}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
