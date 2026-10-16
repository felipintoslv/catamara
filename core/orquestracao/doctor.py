#!/usr/bin/env python3
"""Diagnostico de integridade e ambiente do Catamara (doctor)."""

from __future__ import annotations

import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Any

_CORE_DIR = Path(__file__).resolve().parents[1]
if str(_CORE_DIR) not in sys.path:
    sys.path.insert(0, str(_CORE_DIR))

try:
    from contas import contas as contas_mod
except ImportError:
    contas_mod = None

try:
    from orquestracao.orient import coletar_ollama
except ImportError:
    try:
        from orient import coletar_ollama
    except ImportError:
        coletar_ollama = None


def data_iso_agora() -> str:
    return datetime.now().astimezone().isoformat()


def verificar_binarios_obrigatorios() -> list[dict[str, Any]]:
    obrigatorios = ("bwrap", "agy", "git", "flock", "python3", "jq")
    resultados: list[dict[str, Any]] = []

    for nome in obrigatorios:
        caminho = shutil.which(nome)
        if caminho:
            resultados.append({
                "id": f"binario.{nome}",
                "categoria": "ambiente",
                "nivel": "critico",
                "estado": "ok",
                "detalhe": f"{nome} encontrado em {caminho}",
                "correcao": None,
            })
        else:
            resultados.append({
                "id": f"binario.{nome}",
                "categoria": "ambiente",
                "nivel": "critico",
                "estado": "falha",
                "detalhe": f"binario essencial {nome} nao encontrado no PATH",
                "correcao": f"Instale o pacote contendo {nome} no sistema operacional",
            })

    return resultados


def verificar_binarios_opcionais() -> list[dict[str, Any]]:
    opcionais = ("tmux", "xdg-dbus-proxy", "curl")
    resultados: list[dict[str, Any]] = []

    for nome in opcionais:
        caminho = shutil.which(nome)
        if caminho:
            resultados.append({
                "id": f"binario_opcional.{nome}",
                "categoria": "ambiente",
                "nivel": "aviso",
                "estado": "ok",
                "detalhe": f"{nome} opcional disponivel em {caminho}",
                "correcao": None,
            })
        else:
            resultados.append({
                "id": f"binario_opcional.{nome}",
                "categoria": "ambiente",
                "nivel": "aviso",
                "estado": "aviso",
                "detalhe": f"binario opcional {nome} nao encontrado no PATH",
                "correcao": f"Instale {nome} para suporte pleno a sessoes ou proxy d-bus",
            })

    return resultados


def verificar_bwrap_userns(rapido: bool = False) -> dict[str, Any]:
    if rapido:
        return {
            "id": "bwrap.userns",
            "categoria": "isolamento",
            "nivel": "critico",
            "estado": "ok",
            "detalhe": "Modo rapido: teste de execucao real de userns ignorado",
            "correcao": None,
        }

    if not shutil.which("bwrap"):
        return {
            "id": "bwrap.userns",
            "categoria": "isolamento",
            "nivel": "critico",
            "estado": "falha",
            "detalhe": "bwrap nao esta instalado para testar user namespaces",
            "correcao": "Instale bubblewrap no sistema",
        }

    try:
        proc = subprocess.run(
            ["bwrap", "--ro-bind", "/", "/", "true"],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
        if proc.returncode == 0:
            return {
                "id": "bwrap.userns",
                "categoria": "isolamento",
                "nivel": "critico",
                "estado": "ok",
                "detalhe": "Bubblewrap com suporte a user namespaces operacional",
                "correcao": None,
            }
        err = (proc.stderr or proc.stdout or f"codigo {proc.returncode}").strip()
        return {
            "id": "bwrap.userns",
            "categoria": "isolamento",
            "nivel": "critico",
            "estado": "falha",
            "detalhe": f"Falha ao executar bwrap basico: {err}",
            "correcao": "Verifique a permissao de unprivileged user namespaces no kernel",
        }
    except Exception as exc:
        return {
            "id": "bwrap.userns",
            "categoria": "isolamento",
            "nivel": "critico",
            "estado": "falha",
            "detalhe": f"Excecao ao executar bwrap: {exc}",
            "correcao": "Verifique a configuracao do kernel para unprivileged user namespaces",
        }


def verificar_contas() -> list[dict[str, Any]]:
    resultados: list[dict[str, Any]] = []
    if contas_mod is None:
        resultados.append({
            "id": "contas.modulo",
            "categoria": "contas",
            "nivel": "critico",
            "estado": "falha",
            "detalhe": "Modulo core.contas.contas indisponivel",
            "correcao": "Verifique a integridade do arquivo core/contas/contas.py",
        })
        return resultados

    contas = contas_mod.contas_configuradas()
    estados = [contas_mod.status_conta(c, consultar_remoto=False) for c in contas]
    autenticadas = [s for s in estados if s.get("autenticada")]

    if len(autenticadas) >= 2:
        nomes_autenticadas = ", ".join(s["conta"] for s in autenticadas)
        resultados.append({
            "id": "contas.autenticacao",
            "categoria": "contas",
            "nivel": "critico",
            "estado": "ok",
            "detalhe": f"{len(autenticadas)} contas autenticadas com sucesso ({nomes_autenticadas})",
            "correcao": None,
        })
    elif len(autenticadas) == 1:
        resultados.append({
            "id": "contas.autenticacao",
            "categoria": "contas",
            "nivel": "aviso",
            "estado": "aviso",
            "detalhe": f"Apenas 1 conta autenticada ({autenticadas[0]['conta']}). Revisao cruzada requer 2 contas.",
            "correcao": "Execute 'catamara conta login CONTA' para configurar a segunda conta",
        })
    else:
        resultados.append({
            "id": "contas.autenticacao",
            "categoria": "contas",
            "nivel": "aviso",
            "estado": "aviso",
            "detalhe": "Nenhuma conta configurada esta autenticada.",
            "correcao": "Execute 'catamara conta login <conta>' para configurar a credencial inicial",
        })

    workspaces_por_conta = {c: sorted(contas_mod.ler_workspaces_confiaveis(c)) for c in contas}
    divergencia = False
    primeira_lista = None
    for ws_lista in workspaces_por_conta.values():
        if primeira_lista is None:
            primeira_lista = ws_lista
        elif ws_lista != primeira_lista:
            divergencia = True
            break

    if divergencia:
        resultados.append({
            "id": "contas.confianca",
            "categoria": "contas",
            "nivel": "aviso",
            "estado": "aviso",
            "detalhe": "trustedWorkspaces divergem entre as contas configuradas",
            "correcao": "Execute 'catamara conta confianca sincronizar'",
        })
    else:
        resultados.append({
            "id": "contas.confianca",
            "categoria": "contas",
            "nivel": "aviso",
            "estado": "ok",
            "detalhe": "trustedWorkspaces sincronizados entre as contas configuradas",
            "correcao": None,
        })

    return resultados


def verificar_isolamento_contas() -> list[dict[str, Any]]:
    resultados: list[dict[str, Any]] = []
    if contas_mod is None:
        return resultados

    catamara_path = Path(__file__).resolve().parents[2]
    script_isolar = catamara_path / "bin" / "catamara-isolar"

    if not script_isolar.is_file():
        resultados.append({
            "id": "isolamento.script",
            "categoria": "isolamento",
            "nivel": "critico",
            "estado": "falha",
            "detalhe": f"Script {script_isolar} nao encontrado",
            "correcao": "Restaure o executavel bin/catamara-isolar",
        })
        return resultados

    contas = contas_mod.contas_configuradas()
    contas_dir = str(contas_mod.estado_dir() / "contas")
    home_dir = str(Path.home())
    alvo_bind_agy = f"{home_dir}/.gemini/antigravity-cli"

    for c in contas:
        pasta_c = str(contas_mod.pasta_conta(c))
        cmd = [str(script_isolar), "--mostrar", "--conta", c, "--perfil", "agente", "--", "true"]
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            saida_total = proc.stdout + "\n" + proc.stderr
            if proc.returncode != 0:
                resultados.append({
                    "id": f"isolamento.{c}",
                    "categoria": "isolamento",
                    "nivel": "critico",
                    "estado": "falha",
                    "detalhe": f"catamara-isolar --mostrar retornou codigo {proc.returncode} para {c}: {proc.stderr.strip()}",
                    "correcao": "Verifique os parametros e permissoes de bin/catamara-isolar",
                })
                continue

            if "org.freedesktop.secrets" in saida_total:
                resultados.append({
                    "id": f"isolamento.{c}",
                    "categoria": "isolamento",
                    "nivel": "critico",
                    "estado": "falha",
                    "detalhe": f"Detectado vazamento de org.freedesktop.secrets na configuracao de {c}",
                    "correcao": "Remova qualquer referencia a secrets do filtro d-bus em bin/catamara-isolar",
                })
                continue

            linhas = [ln.strip() for ln in proc.stdout.splitlines()]

            idx_tmpfs_contas = -1
            idx_bind_conta = -1

            for idx, linha in enumerate(linhas):
                if linha == "--tmpfs" and idx + 1 < len(linhas) and linhas[idx + 1] == contas_dir:
                    idx_tmpfs_contas = idx
                if linha == "--bind" and idx + 2 < len(linhas):
                    origem = linhas[idx + 1]
                    destino = linhas[idx + 2]
                    if origem == pasta_c and destino == alvo_bind_agy:
                        idx_bind_conta = idx

            if idx_tmpfs_contas == -1:
                resultados.append({
                    "id": f"isolamento.{c}",
                    "categoria": "isolamento",
                    "nivel": "critico",
                    "estado": "falha",
                    "detalhe": f"Falta isolamento --tmpfs em {contas_dir} para {c}",
                    "correcao": "Adicione --tmpfs na pasta de contas brutas em bin/catamara-isolar",
                })
                continue

            if idx_bind_conta == -1:
                resultados.append({
                    "id": f"isolamento.{c}",
                    "categoria": "isolamento",
                    "nivel": "critico",
                    "estado": "falha",
                    "detalhe": f"Falta montagem --bind de {pasta_c} para {alvo_bind_agy}",
                    "correcao": "Garanta a montagem da conta ativa em ~/.gemini/antigravity-cli",
                })
                continue

            if idx_tmpfs_contas >= idx_bind_conta:
                resultados.append({
                    "id": f"isolamento.{c}",
                    "categoria": "isolamento",
                    "nivel": "critico",
                    "estado": "falha",
                    "detalhe": "A montagem --tmpfs das contas deve vir ANTES do --bind da conta ativa",
                    "correcao": "Ajuste a ordem de argumentos do bwrap em bin/catamara-isolar",
                })
                continue

            outras_contas = [o for o in contas if o != c]
            vazamento_outra = False
            for o in outras_contas:
                pasta_o = str(contas_mod.pasta_conta(o))
                if pasta_o in proc.stdout:
                    vazamento_outra = True
                    break

            if vazamento_outra:
                resultados.append({
                    "id": f"isolamento.{c}",
                    "categoria": "isolamento",
                    "nivel": "critico",
                    "estado": "falha",
                    "detalhe": f"Caminho de outra conta detectado na configuracao de {c}",
                    "correcao": "Garanta que pastas de outras contas permaneçam ocultas",
                })
                continue

            resultados.append({
                "id": f"isolamento.{c}",
                "categoria": "isolamento",
                "nivel": "critico",
                "estado": "ok",
                "detalhe": f"Isolamento bwrap integro para {c} sem vazamento de segredos",
                "correcao": None,
            })

        except Exception as exc:
            resultados.append({
                "id": f"isolamento.{c}",
                "categoria": "isolamento",
                "nivel": "critico",
                "estado": "falha",
                "detalhe": f"Excecao ao verificar isolamento de {c}: {exc}",
                "correcao": "Inspecione o script bin/catamara-isolar",
            })

    return resultados


def verificar_ollama_local() -> dict[str, Any]:
    if coletar_ollama is None:
        return {
            "id": "ollama.status",
            "categoria": "delegacao",
            "nivel": "aviso",
            "estado": "aviso",
            "detalhe": "Funcao coletar_ollama nao importavel de orient.py",
            "correcao": "Verifique core/orquestracao/orient.py",
        }

    ol_dados, _ = coletar_ollama()
    modelo = ol_dados.get("modelo", "qwen3:4b")

    if ol_dados.get("disponivel") and ol_dados.get("modelo_presente"):
        return {
            "id": "ollama.status",
            "categoria": "delegacao",
            "nivel": "aviso",
            "estado": "ok",
            "detalhe": f"Servico Ollama ativo e modelo {modelo} presente",
            "correcao": None,
        }

    motivo = ol_dados.get("motivo") or "indisponivel"
    return {
        "id": "ollama.status",
        "categoria": "delegacao",
        "nivel": "aviso",
        "estado": "aviso",
        "detalhe": f"Delegacao local indisponivel ({motivo})",
        "correcao": f"Inicie o servico Ollama e execute 'ollama pull {modelo}'",
    }


def executar_doctor(rapido: bool = False) -> dict[str, Any]:
    todas_verificacoes: list[dict[str, Any]] = []

    todas_verificacoes.extend(verificar_binarios_obrigatorios())
    todas_verificacoes.extend(verificar_binarios_opcionais())
    todas_verificacoes.append(verificar_bwrap_userns(rapido=rapido))
    todas_verificacoes.extend(verificar_contas())
    todas_verificacoes.extend(verificar_isolamento_contas())
    todas_verificacoes.append(verificar_ollama_local())

    total_ok = sum(1 for v in todas_verificacoes if v["estado"] == "ok")
    total_aviso = sum(1 for v in todas_verificacoes if v["estado"] == "aviso")
    total_falha = sum(1 for v in todas_verificacoes if v["estado"] == "falha")

    codigo_saida = 1 if total_falha > 0 else 0

    return {
        "versao": 1,
        "gerado_em": data_iso_agora(),
        "resumo": {
            "ok": total_ok,
            "aviso": total_aviso,
            "falha": total_falha,
        },
        "codigo_saida": codigo_saida,
        "verificacoes": todas_verificacoes,
    }


def formatar_doctor_texto(dados: dict[str, Any]) -> str:
    linhas: list[str] = []
    linhas.append("=== Diagnostico do Catamara (doctor) ===")
    linhas.append(f"Gerado em: {dados.get('gerado_em')}")
    linhas.append("")

    for v in dados.get("verificacoes", []):
        st = v.get("estado", "").upper()
        nid = v.get("id")
        detalhe = v.get("detalhe", "")
        linhas.append(f"[{st:5s}] {nid}: {detalhe}")
        if v.get("correcao"):
            linhas.append(f"        -> Sugestao: {v.get('correcao')}")

    resumo = dados.get("resumo", {})
    linhas.append("")
    linhas.append(
        f"Resumo: {resumo.get('ok', 0)} ok, {resumo.get('aviso', 0)} avisos, {resumo.get('falha', 0)} falhas."
    )
    return "\n".join(linhas)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="doctor.py",
        description="Diagnostico de integridade e ambiente do Catamara.",
        add_help=True,
    )
    parser.add_argument("--json", action="store_true", help="Gera saida estruturada em JSON.")
    parser.add_argument("--rapido", action="store_true", help="Pula testes demorados ou de execucao real de userns.")

    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return 0 if exc.code == 0 else 2

    resultado = executar_doctor(rapido=args.rapido)

    if args.json:
        print(json.dumps(resultado, ensure_ascii=False, indent=2))
    else:
        print(formatar_doctor_texto(resultado))

    return int(resultado.get("codigo_saida", 0))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
