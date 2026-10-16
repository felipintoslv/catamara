#!/usr/bin/env python3
"""Coleta informacoes de contexto do repositorio, contas, Ollama e sessao."""

from __future__ import annotations

import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.error
import urllib.request
from typing import Any

_CORE_DIR = Path(__file__).resolve().parents[1]
if str(_CORE_DIR) not in sys.path:
    sys.path.insert(0, str(_CORE_DIR))

try:
    from contas import contas as contas_mod
except ImportError:
    contas_mod = None


def data_iso_agora() -> str:
    return datetime.now().astimezone().isoformat()


def _executar_git(args: list[str], cwd: Path, timeout: float = 3.0) -> tuple[int, str, str]:
    cmd = [
        "git",
        "-c", "core.fsmonitor=false",
        "-c", "core.hooksPath=/dev/null",
        "-c", "core.sshCommand=ssh",
        "-c", "core.pager=cat",
        "-c", "safe.bareRepository=explicit",
    ] + args
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        return proc.returncode, proc.stdout.strip(), proc.stderr.strip()
    except subprocess.TimeoutExpired:
        return -1, "", f"timeout apos {timeout}s"
    except Exception as exc:
        return -1, "", str(exc)


def coletar_repo(caminho: str | Path | None = None) -> tuple[dict[str, Any], list[str]]:
    alertas: list[str] = []
    repo_path = Path(caminho).resolve() if caminho else Path.cwd().resolve()

    res_git = {
        "git": False,
        "raiz": str(repo_path),
        "ramo": None,
        "commit_atual": None,
        "commit_base": None,
        "worktree": {"eh_worktree": False, "principal": None},
        "modificados": [],
        "total_modificados": 0,
        "truncado": False,
    }

    rc, out, err = _executar_git(["rev-parse", "--is-inside-work-tree"], cwd=repo_path)
    if rc != 0 or out != "true":
        return res_git, alertas

    res_git["git"] = True

    rc, out, err = _executar_git(["rev-parse", "--show-toplevel"], cwd=repo_path)
    if rc == 0 and out:
        res_git["raiz"] = str(Path(out).resolve())
    else:
        alertas.append(f"git show-toplevel falhou: {err or 'codigo ' + str(rc)}")

    rc, out, err = _executar_git(["branch", "--show-current"], cwd=repo_path)
    if rc == 0 and out:
        res_git["ramo"] = out
    else:
        rc2, out2, _ = _executar_git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=repo_path)
        if rc2 == 0 and out2:
            res_git["ramo"] = out2

    rc, out, err = _executar_git(["rev-parse", "HEAD"], cwd=repo_path)
    if rc == 0 and out:
        res_git["commit_atual"] = out
    else:
        res_git["commit_atual"] = None

    if res_git["commit_atual"]:
        commit_base = None
        for ref_alvo in ("origin/HEAD", "main", "master"):
            rc_mb, out_mb, _ = _executar_git(["merge-base", "HEAD", ref_alvo], cwd=repo_path)
            if rc_mb == 0 and out_mb:
                commit_base = out_mb
                break
        res_git["commit_base"] = commit_base

    rc_gd, out_gd, _ = _executar_git(["rev-parse", "--git-dir"], cwd=repo_path)
    rc_cd, out_cd, _ = _executar_git(["rev-parse", "--git-common-dir"], cwd=repo_path)
    if rc_gd == 0 and rc_cd == 0:
        p_gd = Path(out_gd)
        if not p_gd.is_absolute():
            p_gd = (repo_path / p_gd).resolve()
        else:
            p_gd = p_gd.resolve()

        p_cd = Path(out_cd)
        if not p_cd.is_absolute():
            p_cd = (repo_path / p_cd).resolve()
        else:
            p_cd = p_cd.resolve()

        if p_gd != p_cd:
            res_git["worktree"]["eh_worktree"] = True
            res_git["worktree"]["principal"] = str(p_cd.parent)
        else:
            res_git["worktree"]["eh_worktree"] = False
            res_git["worktree"]["principal"] = res_git["raiz"]

    rc_st, out_st, err_st = _executar_git(["status", "--porcelain=v1", "-z"], cwd=repo_path)
    if rc_st == 0:
        itens_brutos = out_st.split("\0")
        modificados: list[dict[str, str]] = []
        i = 0
        while i < len(itens_brutos):
            item = itens_brutos[i]
            if not item:
                i += 1
                continue
            estado = item[:2].strip() or item[:2]
            resto = item[3:] if len(item) > 3 else ""
            if item.startswith(("R", "C")) and i + 1 < len(itens_brutos):
                destino = itens_brutos[i + 1]
                modificados.append({"estado": estado, "caminho": f"{resto} -> {destino}"})
                i += 2
                continue
            modificados.append({"estado": estado, "caminho": resto})
            i += 1

        total = len(modificados)
        res_git["total_modificados"] = total
        res_git["truncado"] = total > 50
        res_git["modificados"] = modificados[:50]
    else:
        alertas.append(f"git status falhou: {err_st or 'codigo ' + str(rc_st)}")

    return res_git, alertas


def coletar_contas() -> tuple[list[dict[str, Any]], list[str]]:
    alertas: list[str] = []
    lista_contas: list[dict[str, Any]] = []

    if contas_mod is None:
        alertas.append("modulo core.contas.contas nao disponivel para importacao")
        return lista_contas, alertas

    agora = int(time.time())
    try:
        contas_cfg = contas_mod.contas_configuradas()
    except Exception as exc:
        alertas.append(f"erro ao obter contas configuradas: {exc}")
        contas_cfg = ["conta1", "conta2"]

    for conta_nome in contas_cfg:
        try:
            st = contas_mod.status_conta(conta_nome, consultar_remoto=False)
            resumo_cotas = st.get("resumo_cotas", {})
            cotas_dict = st.get("cotas", {})
            ts = cotas_dict.get("atualizado_em_epoch") if isinstance(cotas_dict, dict) else None
            idade_s = (agora - int(ts)) if ts is not None else None

            lista_contas.append({
                "nome": conta_nome,
                "inicializada": bool(st.get("inicializada")),
                "autenticada": bool(st.get("autenticada")),
                "email": st.get("email"),
                "cotas": {
                    "argon": resumo_cotas.get("argon_pct"),
                    "gemini": resumo_cotas.get("gemini_pct"),
                    "3p": resumo_cotas.get("3p_pct"),
                },
                "cache_idade_s": idade_s,
            })
        except Exception as exc:
            alertas.append(f"erro ao consultar status da conta {conta_nome}: {exc}")
            lista_contas.append({
                "nome": conta_nome,
                "inicializada": False,
                "autenticada": False,
                "email": None,
                "cotas": {"argon": None, "gemini": None, "3p": None},
                "cache_idade_s": None,
            })

    return lista_contas, alertas


def coletar_ollama() -> tuple[dict[str, Any], list[str]]:
    alertas: list[str] = []
    url = os.environ.get("CATAMARA_OLLAMA_URL", "http://localhost:11434")
    modelo = os.environ.get("CATAMARA_LOCAL_MODELO", "qwen3:4b")
    try:
        vram_min = int(os.environ.get("CATAMARA_LOCAL_VRAM_MIN", "4000"))
    except ValueError:
        vram_min = 4000

    disponivel = False
    modelo_presente = False
    motivo = ""

    try:
        req = urllib.request.Request(
            f"{url.rstrip('/')}/api/tags",
            headers={"User-Agent": "catamara-orient/1.0"},
        )
        with urllib.request.urlopen(req, timeout=1.5) as resp:
            if resp.status == 200:
                disponivel = True
                conteudo = json.loads(resp.read().decode("utf-8"))
                modelos = [m.get("name", "") for m in conteudo.get("models", []) if isinstance(m, dict)]
                modelo_presente = any(
                    m == modelo or m == f"{modelo}:latest" or m.startswith(f"{modelo}:")
                    for m in modelos
                )
                if not modelo_presente:
                    motivo = "modelo_ausente"
    except Exception as exc:
        disponivel = False
        modelo_presente = False
        motivo = "ollama_inacessivel"

    estado_path = Path(os.environ.get("CATAMARA_ESTADO", os.path.expanduser("~/.local/state/catamara")))
    vram_json_path = estado_path / "marcas" / "vram.json"
    vram_livre_mib: int | None = None
    if vram_json_path.is_file():
        try:
            d_vram = json.loads(vram_json_path.read_text(encoding="utf-8"))
            if isinstance(d_vram.get("livre_mib"), (int, float)):
                vram_livre_mib = int(d_vram["livre_mib"])
        except Exception as exc:
            alertas.append(f"falha ao ler marca de vram: {exc}")

    jogo_json_path = estado_path / "marcas" / "jogo.json"
    jogo_ativo = False
    if jogo_json_path.is_file():
        try:
            d_jogo = json.loads(jogo_json_path.read_text(encoding="utf-8"))
            jogo_ativo = bool(d_jogo.get("ativo", False))
        except Exception as exc:
            alertas.append(f"falha ao ler marca de jogo: {exc}")

    if jogo_ativo:
        motivo = "jogo_ativo"
    elif vram_livre_mib is not None and vram_livre_mib < vram_min:
        motivo = "vram_insuficiente"

    recomendado = bool(
        disponivel and modelo_presente and not jogo_ativo and (vram_livre_mib is None or vram_livre_mib >= vram_min)
    )

    dados_ollama = {
        "disponivel": disponivel,
        "modelo": modelo,
        "modelo_presente": modelo_presente,
        "vram_livre_mib": vram_livre_mib,
        "jogo_ativo": jogo_ativo,
        "recomendado": recomendado,
        "motivo": motivo,
    }
    return dados_ollama, alertas


def coletar_sessao() -> dict[str, Any]:
    return {
        "id": os.environ.get("CATAMARA_SESSAO"),
        "conta": os.environ.get("CATAMARA_CONTA"),
        "isolado": os.environ.get("CATAMARA_ISOLADO") == "1",
        "perfil": os.environ.get("CATAMARA_PERFIL"),
    }


def coletar_historico(ultimas: int = 5) -> tuple[dict[str, list[dict[str, Any]]], list[str]]:
    alertas: list[str] = []
    estado_path = Path(os.environ.get("CATAMARA_ESTADO", os.path.expanduser("~/.local/state/catamara")))

    def _ler_ultimas_linhas(caminho: Path) -> list[dict[str, Any]]:
        if not caminho.is_file():
            return []
        try:
            linhas = [ln.strip() for ln in caminho.read_text(encoding="utf-8").splitlines() if ln.strip()]
            finais = linhas[-ultimas:] if ultimas > 0 else linhas
            registros: list[dict[str, Any]] = []
            for ln in finais:
                try:
                    obj = json.loads(ln)
                    if isinstance(obj, dict):
                        registros.append(obj)
                except Exception:
                    pass
            return registros
        except Exception as exc:
            alertas.append(f"erro ao ler historico de {caminho.name}: {exc}")
            return []

    historico = {
        "delegacoes": _ler_ultimas_linhas(estado_path / "delegacoes.jsonl"),
        "validar": _ler_ultimas_linhas(estado_path / "validar.jsonl"),
    }
    return historico, alertas


def coletar_orient(repo_path: str | Path | None = None, ultimas: int = 5) -> dict[str, Any]:
    todos_alertas: list[str] = []

    repo_dados, a_repo = coletar_repo(repo_path)
    todos_alertas.extend(a_repo)

    contas_dados, a_contas = coletar_contas()
    todos_alertas.extend(a_contas)

    ollama_dados, a_ollama = coletar_ollama()
    todos_alertas.extend(a_ollama)

    sessao_dados = coletar_sessao()

    historico_dados, a_hist = coletar_historico(ultimas)
    todos_alertas.extend(a_hist)

    return {
        "versao": 1,
        "gerado_em": data_iso_agora(),
        "repo": repo_dados,
        "sessao": sessao_dados,
        "contas": contas_dados,
        "ollama": ollama_dados,
        "historico": historico_dados,
        "alertas": todos_alertas,
    }


def formatar_texto_legivel(dados: dict[str, Any]) -> str:
    linhas: list[str] = []
    linhas.append("=== Contexto Catamara (orient) ===")
    linhas.append(f"Gerado em: {dados.get('gerado_em')}")

    repo = dados.get("repo", {})
    if repo.get("git"):
        linhas.append(f"\n[Repositorio] {repo.get('raiz')}")
        linhas.append(f"  Ramo: {repo.get('ramo')} | Commit: {repo.get('commit_atual') or 'nenhum'}")
        linhas.append(f"  Base: {repo.get('commit_base') or 'nenhum'}")
        if repo.get("worktree", {}).get("eh_worktree"):
            linhas.append(f"  Worktree: sim (principal: {repo['worktree'].get('principal')})")
        total_mod = repo.get("total_modificados", 0)
        linhas.append(f"  Arquivos modificados: {total_mod}")
    else:
        linhas.append(f"\n[Diretorio] {repo.get('raiz')} (fora de repositorio git)")

    sessao = dados.get("sessao", {})
    linhas.append("\n[Sessao]")
    linhas.append(f"  ID: {sessao.get('id') or 'nenhuma'} | Conta: {sessao.get('conta') or 'nenhuma'}")
    linhas.append(f"  Isolado: {'sim' if sessao.get('isolado') else 'nao'} | Perfil: {sessao.get('perfil') or 'nenhum'}")

    linhas.append("\n[Contas]")
    for c in dados.get("contas", []):
        status_str = "autenticada" if c.get("autenticada") else ("inicializada" if c.get("inicializada") else "pendente")
        cotas = c.get("cotas", {})
        c_str = f"argon={cotas.get('argon')}% gemini={cotas.get('gemini')}% 3p={cotas.get('3p')}%"
        linhas.append(f"  - {c.get('nome')}: {status_str} ({c.get('email') or 'sem email'}) [{c_str}]")

    ollama = dados.get("ollama", {})
    linhas.append("\n[Ollama Local]")
    linhas.append(f"  Modelo: {ollama.get('modelo')} | Presente: {'sim' if ollama.get('modelo_presente') else 'nao'}")
    linhas.append(f"  Disponivel: {'sim' if ollama.get('disponivel') else 'nao'} | Recomendado: {'sim' if ollama.get('recomendado') else 'nao'}")
    if ollama.get("motivo"):
        linhas.append(f"  Motivo: {ollama.get('motivo')}")

    alertas = dados.get("alertas", [])
    if alertas:
        linhas.append("\n[Alertas]")
        for a in alertas:
            linhas.append(f"  ! {a}")

    return "\n".join(linhas)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="orient.py",
        description="Coleta informacoes de orientacao para sessoes e agentes do Catamara.",
        add_help=True,
    )
    parser.add_argument("--json", action="store_true", help="Gera saida estruturada em JSON.")
    parser.add_argument("--repo", type=str, default=None, help="Caminho do repositorio a analisar.")
    parser.add_argument("--ultimas", type=int, default=5, help="Quantidade de registros historicos a carregar.")

    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return 0 if exc.code == 0 else 2

    resultado = coletar_orient(repo_path=args.repo, ultimas=args.ultimas)

    if args.json:
        print(json.dumps(resultado, ensure_ascii=False, indent=2))
    else:
        print(formatar_texto_legivel(resultado))

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
