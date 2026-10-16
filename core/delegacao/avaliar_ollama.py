#!/usr/bin/env python3
"""Avaliacao de disponibilidade e execucao local via Ollama para leitor e redator."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from typing import Any

_raiz_catamara = str(Path(__file__).resolve().parents[2])
if _raiz_catamara not in sys.path:
    sys.path.insert(0, _raiz_catamara)

from core.delegacao import extracao

FAIXA_RE = re.compile(r"^(.+):(\d+)-(\d+)$")


def estado_dir() -> Path:
    return Path(
        os.environ.get(
            "CATAMARA_ESTADO",
            os.path.expanduser("~/.local/state/catamara"),
        )
    )


def ollama_url() -> str:
    return os.environ.get("CATAMARA_OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/")


def local_modelo() -> str:
    return os.environ.get("CATAMARA_LOCAL_MODELO", "qwen3:4b")


def local_vram_min() -> int:
    try:
        return int(os.environ.get("CATAMARA_LOCAL_VRAM_MIN", "3500"))
    except ValueError:
        return 3500


def local_fatias_max() -> int:
    try:
        return int(os.environ.get("CATAMARA_LOCAL_FATIAS_MAX", "8"))
    except ValueError:
        return 8


def local_caracteres_max() -> int:
    try:
        return int(os.environ.get("CATAMARA_LOCAL_CARACTERES_MAX", "24000"))
    except ValueError:
        return 24000


def local_ctx() -> int:
    try:
        return int(os.environ.get("CATAMARA_LOCAL_CTX", "8192"))
    except ValueError:
        return 8192


def local_espera_padrao() -> int:
    try:
        return int(os.environ.get("CATAMARA_LOCAL_ESPERA", "30"))
    except ValueError:
        return 30


def _ler_marca_recente(caminho: Path, max_idade_s: int = 120) -> dict[str, Any] | None:
    if not caminho.is_file() or caminho.is_symlink():
        return None
    try:
        st = caminho.stat()
        if (time.time() - st.st_mtime) > max_idade_s:
            return None
        dados = json.loads(caminho.read_text(encoding="utf-8"))
        if isinstance(dados, dict):
            return dados
    except Exception:
        pass
    return None


def obter_vram_livre() -> int | None:
    """Tenta obter a VRAM livre em MB via marca ou utilitario vram-livre."""
    marca = _ler_marca_recente(estado_dir() / "marcas" / "vram.json")
    if marca and isinstance(marca.get("livre_mb"), (int, float)):
        return int(marca["livre_mb"])

    cmd_vram = shutil.which("vram-livre")
    if cmd_vram:
        try:
            res = subprocess.run([cmd_vram], capture_output=True, text=True, timeout=2)
            if res.returncode == 0:
                s = res.stdout.strip().split()[0]
                return int(s)
        except Exception:
            pass
    return None


def verificar_disponibilidade() -> tuple[bool, str | None, int | None]:
    """Verifica disponibilidade do Ollama retornando (disponivel, motivo, vram_livre_mb)."""
    # 1. Checa jogo ativo
    jogo = _ler_marca_recente(estado_dir() / "marcas" / "jogo.json")
    if jogo and jogo.get("ativo") is True:
        return False, "jogo_ativo", obter_vram_livre()

    # 2. Checa VRAM
    vram_livre = obter_vram_livre()
    if vram_livre is not None and vram_livre < local_vram_min():
        return False, "vram_insuficiente", vram_livre

    # 3. GET /api/tags com timeout de 2 segundos
    url = f"{ollama_url()}/api/tags"
    req = urllib.request.Request(url, headers={"User-Agent": "catamara/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=2.0) as resp:
            if resp.status != 200:
                return False, "ollama_inacessivel", vram_livre
            corpo = resp.read().decode("utf-8")
            dados = json.loads(corpo)
    except Exception:
        return False, "ollama_inacessivel", vram_livre

    modelos = [m.get("name", "") for m in dados.get("models", []) if isinstance(m, dict)]
    alvo = local_modelo()
    # Casamento de modelo: exato, sem tag ou prefixo
    encontrado = False
    for m in modelos:
        if m == alvo or m == f"{alvo}:latest" or m.startswith(f"{alvo}:"):
            encontrado = True
            break
        if alvo.endswith(":latest") and m == alvo[:-7]:
            encontrado = True
            break

    if not encontrado:
        return False, "modelo_ausente", vram_livre

    return True, None, vram_livre


def carregar_prompt_sistema(papel: str) -> str:
    """Carrega o corpo do agente em core/agentes/agy/agents/<papel>.md."""
    raiz = Path(__file__).resolve().parents[2]
    caminho = raiz / "core" / "agentes" / "agy" / "agents" / f"{papel}.md"
    if caminho.is_file():
        conteudo = caminho.read_text(encoding="utf-8")
        if conteudo.startswith("---"):
            partes = conteudo.split("---", 2)
            if len(partes) >= 3:
                return partes[2].strip()
        return conteudo.strip()

    if papel == "leitor":
        return "Voce e o leitor. Responda apenas com base nos documentos citando arquivo:linha."
    return "Voce e o redator. Produza o texto solicitado de forma concisa e sem inventar dados."


def extrair_texto_fatia(alvo: str) -> tuple[str, str]:
    """Extrai o conteudo de um alvo (ARQ ou ARQ:INI-FIM), retornando (rotulo, texto)."""
    m = FAIXA_RE.match(alvo)
    if m:
        caminho_arq = m.group(1)
        ini = int(m.group(2))
        fim = int(m.group(3))
    else:
        caminho_arq = alvo
        ini = None
        fim = None

    p = Path(caminho_arq)
    if not p.is_file():
        raise FileNotFoundError(f"arquivo nao encontrado: {caminho_arq}")

    # Detecta se eh PDF
    eh_pdf = p.suffix.lower() == ".pdf"
    if not eh_pdf and shutil.which("file"):
        try:
            m_res = subprocess.run(["file", "-b", "--mime-type", str(p)], capture_output=True, text=True)
            if "pdf" in m_res.stdout.lower():
                eh_pdf = True
        except Exception:
            pass

    if eh_pdf:
        if not shutil.which("pdftotext"):
            raise RuntimeError("pdftotext_ausente")
        cmd = ["pdftotext", "-layout"]
        if ini is not None and fim is not None:
            cmd.extend(["-f", str(ini), "-l", str(fim)])
        cmd.extend([str(p), "-"])
        res = subprocess.run(cmd, capture_output=True, text=True)
        if res.returncode != 0:
            raise RuntimeError(f"erro ao executar pdftotext: {res.stderr.strip()}")
        texto = res.stdout
    else:
        linhas = p.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
        if ini is not None and fim is not None:
            linhas_selecionadas = linhas[max(0, ini - 1) : max(0, fim)]
        else:
            linhas_selecionadas = linhas
        texto = "".join(linhas_selecionadas)

    limite = local_caracteres_max()
    if len(texto) > limite:
        texto = texto[:limite]

    return alvo, texto


def chamar_ollama_chat(
    mensagens: list[dict[str, str]],
    temperatura: float,
    seed: int | None = None,
    formato_json: dict[str, Any] | None = None,
    timeout_s: float = 300.0,
) -> str:
    """Faz requisicao POST /api/chat ao Ollama."""
    url = f"{ollama_url()}/api/chat"
    payload: dict[str, Any] = {
        "model": local_modelo(),
        "messages": mensagens,
        "stream": False,
        "options": {
            "temperature": temperatura,
            "num_ctx": local_ctx(),
        },
    }
    if seed is not None:
        payload["options"]["seed"] = seed
    if formato_json is not None:
        payload["format"] = formato_json

    corpo_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=corpo_bytes,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "catamara/1.0",
        },
        method="POST",
    )

    with urllib.request.urlopen(req, timeout=timeout_s) as resp:
        if resp.status != 200:
            raise RuntimeError(f"erro HTTP no Ollama: {resp.status}")
        resposta_bruta = resp.read().decode("utf-8")
        dados = json.loads(resposta_bruta)
        conteudo = dados.get("message", {}).get("content", "")
        return conteudo


def executar_com_trava(
    papel: str,
    tarefa: str,
    alvos: list[str],
    declaracoes_extrair: list[str],
    declaracoes_agregar: list[str],
    espera_segundos: int,
    tempo_max_s: float,
) -> int:
    """Executa a tarefa sob trava local.lock."""
    if papel not in {"leitor", "redator"}:
        print(f"papel invalido para avaliar_ollama: {papel}", file=sys.stderr)
        return 2

    # Verifica limite de fatias antes da trava
    fatias_max = local_fatias_max()
    if len(alvos) > fatias_max:
        print(
            f"excesso de fatias ({len(alvos)} > {fatias_max})",
            file=sys.stderr,
        )
        return 3

    # Adquire trava com timeout
    lock_path = estado_dir() / "local.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    inicio = time.time()
    travado = False
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                travado = True
                break
            except (BlockingIOError, OSError):
                if time.time() - inicio >= espera_segundos:
                    break
                time.sleep(0.5)

        if not travado:
            print("recurso ocupado (timeout de espera pela trava local.lock)", file=sys.stderr)
            return 3

        # Sob a trava, confere disponibilidade novamente
        disp, motivo, _ = verificar_disponibilidade()
        if not disp:
            print(f"ollama indisponivel sob trava: {motivo}", file=sys.stderr)
            return 3

        # Processa fatias
        fatias_conteudo: list[tuple[str, str]] = []
        for alvo in alvos:
            try:
                rotulo, texto = extrair_texto_fatia(alvo)
                fatias_conteudo.append((rotulo, texto))
            except RuntimeError as exc_rt:
                if str(exc_rt) == "pdftotext_ausente":
                    print("pdftotext nao instalado para leitura de PDF", file=sys.stderr)
                    return 3
                print(f"erro ao ler fatia {alvo}: {exc_rt}", file=sys.stderr)
                return 1
            except Exception as exc_leitura:
                print(f"erro ao ler fatia {alvo}: {exc_leitura}", file=sys.stderr)
                return 1

        prompt_sistema = carregar_prompt_sistema(papel)
        temperatura = 0.0 if papel == "leitor" else 0.2
        seed = 0 if papel == "leitor" else None

        # Rota com extracao estruturada
        if declaracoes_extrair:
            try:
                campos = extracao.validar_esquema(declaracoes_extrair)
                schema_json = extracao.esquema_para_json(campos)
            except Exception as exc_esq:
                print(f"esquema invalido para extracao: {exc_esq}", file=sys.stderr)
                return 2

            with tempfile.TemporaryDirectory(prefix="catamara-extracao-") as tmpdir:
                arquivos_fatias: list[str] = []
                # Se nao houver alvos, roda uma chamada vazia com a tarefa
                lista_fatias = fatias_conteudo or [("tarefa", "")]
                for idx, (rotulo, texto) in enumerate(lista_fatias):
                    prompt_usuario = f"Tarefa: {tarefa}\n\nDocumento ({rotulo}):\n{texto}"
                    mensagens = [
                        {"role": "system", "content": prompt_sistema},
                        {"role": "user", "content": prompt_usuario},
                    ]

                    # 1 tentativa + 1 re-tentativa se JSON for invalido
                    sucesso_json = False
                    resposta_texto = ""
                    for tentativa in range(2):
                        try:
                            resposta_texto = chamar_ollama_chat(
                                mensagens,
                                temperatura=temperatura,
                                seed=seed,
                                formato_json=schema_json,
                                timeout_s=tempo_max_s,
                            )
                            # Valida se eh JSON com registros
                            parsed = json.loads(resposta_texto)
                            if isinstance(parsed, dict) and "registros" in parsed:
                                sucesso_json = True
                                break
                        except Exception:
                            pass

                    if not sucesso_json:
                        # Prepara registro vazio para a fatia
                        dados_fatia = {"registros": []}
                    else:
                        try:
                            dados_fatia = json.loads(resposta_texto)
                        except Exception:
                            dados_fatia = {"registros": []}

                    caminho_tmp = Path(tmpdir) / f"fatia_{idx}.json"
                    caminho_tmp.write_text(json.dumps(dados_fatia, ensure_ascii=False), encoding="utf-8")
                    arquivos_fatias.append(str(caminho_tmp))

                # Consolida as fatias
                try:
                    resultado = extracao.consolidar_arquivos(
                        arquivos_fatias,
                        declaracoes_extrair,
                        declaracoes_agregar,
                    )
                    print(json.dumps(resultado, ensure_ascii=False))
                    return 0
                except Exception as exc_cons:
                    print(f"falha ao consolidar extracao: {exc_cons}", file=sys.stderr)
                    return 1

        # Rota sem extracao
        if papel == "leitor":
            respostas_blocos: list[str] = []
            lista_fatias = fatias_conteudo or [("documento", "")]
            for rotulo, texto in lista_fatias:
                prompt_usuario = f"Tarefa: {tarefa}\n\nDocumento ({rotulo}):\n{texto}"
                mensagens = [
                    {"role": "system", "content": prompt_sistema},
                    {"role": "user", "content": prompt_usuario},
                ]
                try:
                    resp = chamar_ollama_chat(
                        mensagens,
                        temperatura=temperatura,
                        seed=seed,
                        timeout_s=tempo_max_s,
                    )
                    respostas_blocos.append(f"## {rotulo}\n\n{resp.strip()}\n")
                except Exception as exc_chat:
                    print(f"erro na chamada Ollama para {rotulo}: {exc_chat}", file=sys.stderr)
                    return 1

            saida_final = "\n".join(respostas_blocos).rstrip() + "\n"
            sys.stdout.write(saida_final)
            return 0

        # papel == "redator"
        blocos: list[str] = []
        for rotulo, texto in fatias_conteudo:
            blocos.append(f"--- Documento: {rotulo} ---\n{texto}")
        fatias_juntas = "\n\n".join(blocos)
        prompt_usuario = f"Tarefa: {tarefa}\n\n{fatias_juntas}".strip()
        mensagens = [
            {"role": "system", "content": prompt_sistema},
            {"role": "user", "content": prompt_usuario},
        ]
        try:
            resp = chamar_ollama_chat(
                mensagens,
                temperatura=temperatura,
                seed=seed,
                timeout_s=tempo_max_s,
            )
            sys.stdout.write(resp.rstrip() + "\n")
            return 0
        except Exception as exc_chat:
            print(f"erro na chamada Ollama para redator: {exc_chat}", file=sys.stderr)
            return 1

    finally:
        if travado:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except Exception:
                pass
        os.close(fd)


def main(argv: list[str] | None = None) -> int:
    args_list = sys.argv[1:] if argv is None else argv
    if not args_list:
        print("uso: avaliar_ollama.py [disponivel | executar] ...", file=sys.stderr)
        return 2

    subcmd = args_list[0]
    restante = args_list[1:]

    if subcmd == "disponivel":
        formato_json = "--json" in restante or True
        disp, motivo, vram = verificar_disponibilidade()
        info = {
            "disponivel": disp,
            "motivo": motivo,
            "modelo": local_modelo(),
            "vram_livre_mb": vram,
        }
        if formato_json:
            print(json.dumps(info, ensure_ascii=False))
        return 0 if disp else 3

    if subcmd == "executar":
        parser = argparse.ArgumentParser(prog="avaliar_ollama.py executar")
        parser.add_argument("--papel", required=True, choices=["leitor", "redator"])
        parser.add_argument("--tarefa", required=True)
        parser.add_argument("--extrair", action="append", default=[])
        parser.add_argument("--agregar", action="append", default=[])
        parser.add_argument("--espera", type=int, default=local_espera_padrao())
        parser.add_argument("--tempo-max", type=float, default=300.0)
        parser.add_argument("alvos", nargs="*")

        try:
            args = parser.parse_args(restante)
        except Exception as exc_parse:
            print(f"argumentos invalidos: {exc_parse}", file=sys.stderr)
            return 2

        # Desmembra itens passados com multiplos espacos em --extrair e --agregar
        extrair_plano: list[str] = []
        for item in args.extrair:
            extrair_plano.extend(item.split())

        agregar_plano: list[str] = []
        for item in args.agregar:
            agregar_plano.extend(item.split())

        return executar_com_trava(
            papel=args.papel,
            tarefa=args.tarefa,
            alvos=args.alvos,
            declaracoes_extrair=extrair_plano,
            declaracoes_agregar=agregar_plano,
            espera_segundos=args.espera,
            tempo_max_s=args.tempo_max,
        )

    print(f"subcomando desconhecido: {subcmd}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
