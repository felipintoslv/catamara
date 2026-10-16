#!/usr/bin/env python3
"""Gerenciador de sessoes de desenvolvimento do Catamara com tmux e git worktrees.

Suporta:
- iniciar (ou run): cria worktree isolada, registra sessao e inicia container tmux
- continuar (ou continue): anexa a sessao existente ou a recria
- listar (ou list): lista todas as sessoes conhecidas e seus estados
- encerrar (ou stop): encerra sessao tmux e opcionalmente limpa worktree
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import time
from typing import Any

NOME_SESSAO_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,40}$")


def data_iso_atual() -> str:
    return datetime.now(timezone.utc).isoformat()


def obter_diretorios() -> dict[str, Path]:
    catamara_path = Path(
        os.environ.get(
            "CATAMARA_PATH",
            str(Path(__file__).resolve().parents[2]),
        )
    )
    estado_dir = Path(
        os.environ.get(
            "CATAMARA_ESTADO",
            os.path.expanduser("~/.local/state/catamara"),
        )
    )
    sessoes_dir = Path(
        os.environ.get(
            "CATAMARA_SESSOES",
            str(estado_dir / "sessoes"),
        )
    )
    worktrees_dir = Path(
        os.environ.get(
            "CATAMARA_WORKTREES",
            os.path.expanduser("~/.local/share/catamara-worktrees"),
        )
    )
    core_py = Path(
        os.environ.get(
            "CATAMARA_CORE_PY",
            str(catamara_path / "core"),
        )
    )
    projetos_dir = Path(
        os.environ.get(
            "CATAMARA_PROJETOS",
            Path.home() / "Projetos",
        )
    )
    return {
        "path": catamara_path,
        "estado": estado_dir,
        "sessoes": sessoes_dir,
        "worktrees": worktrees_dir,
        "core_py": core_py,
        "projetos": projetos_dir,
    }


def inicializar_novo_repo_git(caminho: Path, nome_projeto: str) -> Path:
    """Inicializa um novo repositorio Git com commit inicial limpo."""
    if caminho.is_file() and caminho.stat().st_size == 0:
        caminho.unlink()
    caminho.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "-C", str(caminho), "init", "-b", "main"], capture_output=True, check=False)
    subprocess.run(["git", "-C", str(caminho), "checkout", "-B", "main"], capture_output=True, check=False)
    readme = caminho / "README.md"
    if not readme.exists():
        readme.write_text(f"# {nome_projeto}\n\nProjeto gerenciado com Catamara.\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(caminho), "add", "README.md"], capture_output=True, check=False)
        subprocess.run(
            ["git", "-C", str(caminho), "commit", "-m", "feat: commit inicial do projeto"],
            capture_output=True,
            check=False,
        )
    return caminho.resolve()


def obter_raiz_git(caminho: Path) -> Path | None:
    try:
        proc = subprocess.run(
            ["git", "-C", str(caminho), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            return Path(proc.stdout.strip()).resolve()
    except Exception:
        pass
    return None


def validar_id_sessao(nome: str) -> str:
    if not NOME_SESSAO_RE.match(nome):
        raise ValueError(
            f"Nome de sessao invalido: {nome!r}. Deve conter apenas letras minusculas, numeros e hifens."
        )
    return nome


def tmux_has_session(socket: str, nome: str) -> bool:
    cmd = ["tmux", "-L", socket, "has-session", "-t", nome]
    proc = subprocess.run(cmd, capture_output=True, check=False)
    return proc.returncode == 0


def subcomando_listar(args: argparse.Namespace) -> int:
    dirs = obter_diretorios()
    sessoes_dir = dirs["sessoes"]
    sessoes_dir.mkdir(parents=True, exist_ok=True)

    arquivos = sorted(sessoes_dir.glob("*.json"))
    lista_sessoes: list[dict[str, Any]] = []

    for arq in arquivos:
        try:
            dados = json.loads(arq.read_text(encoding="utf-8"))
            if not isinstance(dados, dict):
                continue
            sessao_id = dados.get("sessao") or arq.stem
            tmux_id = dados.get("tmux") or sessao_id

            # Confere no tmux se a sessao ainda esta rodando
            ativa_no_tmux = tmux_has_session("catamara", tmux_id)
            if dados.get("estado") == "ativa" and not ativa_no_tmux:
                dados["estado"] = "inativa"
            elif ativa_no_tmux:
                dados["estado"] = "ativa"

            lista_sessoes.append(dados)
        except Exception:
            continue

    if args.formato_json:
        print(json.dumps({"sessoes": lista_sessoes}, ensure_ascii=False, indent=2))
        return 0

    if not lista_sessoes:
        print("Nenhuma sessao encontrada.")
        return 0

    print(f"{'SESSAO':<20} {'ESTADO':<10} {'CONTA':<10} {'PROJETO':<18} {'CRIADO EM'}")
    print("-" * 75)
    for s in lista_sessoes:
        sid = str(s.get("sessao", ""))[:18]
        est = str(s.get("estado", ""))[:8]
        cnt = str(s.get("conta", ""))[:9]
        prj = str(s.get("projeto", ""))[:16]
        cri = str(s.get("criado", ""))[:19]
        print(f"{sid:<20} {est:<10} {cnt:<10} {prj:<18} {cri}")

    return 0


def escolher_conta_autor(dirs: dict[str, Path], modelo: str) -> str:
    # Chama contas.py escolher --papel autor
    contas_script = dirs["core_py"] / "contas" / "contas.py"
    if not contas_script.is_file():
        return os.environ.get("CATAMARA_CONTA_PADRAO", "conta1")
    try:
        proc = subprocess.run(
            [
                sys.executable,
                str(contas_script),
                "escolher",
                "--papel",
                "autor",
                "--modelo",
                modelo,
                "--apenas-nome",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            return proc.stdout.strip()
    except Exception:
        pass
    return os.environ.get("CATAMARA_CONTA_PADRAO", "conta1")


def adicionar_confianca(dirs: dict[str, Path], caminho: Path) -> None:
    contas_script = dirs["core_py"] / "contas" / "contas.py"
    if not contas_script.is_file():
        return
    try:
        subprocess.run(
            [sys.executable, str(contas_script), "confianca", "adicionar", str(caminho)],
            capture_output=True,
            check=False,
        )
    except Exception:
        pass


def criar_e_iniciar_sessao(
    repo: str | Path,
    sessao_id: str | None = None,
    conta: str | None = None,
    direto: bool = False,
    forcar: bool = False,
    dry_run: bool = False,
    sem_tmux: bool = False,
    criar_se_nao_existir: bool = True,
) -> dict[str, Any]:
    dirs = obter_diretorios()
    repo_str = str(repo).strip()
    if not repo_str or repo_str == ".":
        caminho_repo = Path.cwd().resolve()
    elif not os.path.isabs(repo_str) and not repo_str.startswith("~") and "/" not in repo_str:
        caminho_repo = (dirs["projetos"] / repo_str).resolve()
    else:
        caminho_repo = Path(os.path.expanduser(repo_str)).resolve()

    repo_raiz = obter_raiz_git(caminho_repo)
    if not repo_raiz:
        if criar_se_nao_existir:
            nome_proj = caminho_repo.name
            repo_raiz = inicializar_novo_repo_git(caminho_repo, nome_proj)
        else:
            raise ValueError(f"{caminho_repo} nao pertence a um repositorio Git valido.")

    projeto_nome = repo_raiz.name.lower()
    projeto_nome = re.sub(r"[^a-z0-9]", "-", projeto_nome).strip("-") or "projeto"

    if not sessao_id:
        ts = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
        sessao_id = f"{projeto_nome}-{ts}"

    validar_id_sessao(sessao_id)

    sessao_json = dirs["sessoes"] / f"{sessao_id}.json"
    if sessao_json.exists() and not forcar:
        raise RuntimeError(f"Sessao {sessao_id} ja existe. Use outro nome ou reative-a.")

    autor_modelo = os.environ.get("CATAMARA_AUTOR_MODELO") or "gemini-4-argon"
    autor_esforco = os.environ.get("CATAMARA_AUTOR_ESFORCO", "medium")

    if not conta:
        conta = escolher_conta_autor(dirs, autor_modelo)

    if direto:
        caminho_worktree = repo_raiz
        ramo_git = None
    else:
        dirs["worktrees"].mkdir(parents=True, exist_ok=True)
        caminho_worktree = dirs["worktrees"] / f"{projeto_nome}-{sessao_id}"
        ramo_git = f"catamara/{sessao_id}"

        if not dry_run and not caminho_worktree.exists():
            proc_wt = subprocess.run(
                [
                    "git",
                    "-C",
                    str(repo_raiz),
                    "worktree",
                    "add",
                    "-b",
                    ramo_git,
                    str(caminho_worktree),
                    "HEAD",
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            if proc_wt.returncode != 0:
                raise RuntimeError(
                    f"Erro ao criar git worktree em {caminho_worktree}: {proc_wt.stderr.strip()}"
                )

    if not dry_run:
        adicionar_confianca(dirs, caminho_worktree)

    tmux_nome = f"{projeto_nome}-{sessao_id}"
    dados_sessao = {
        "sessao": sessao_id,
        "projeto": projeto_nome,
        "repo": str(repo_raiz),
        "worktree": str(caminho_worktree),
        "ramo": ramo_git,
        "conta": conta,
        "modelo": autor_modelo,
        "esforco": autor_esforco,
        "tmux": tmux_nome,
        "criado": data_iso_atual(),
        "estado": "ativa",
    }

    if not dry_run:
        dirs["sessoes"].mkdir(parents=True, exist_ok=True)
        sessao_json.write_text(
            json.dumps(dados_sessao, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    catamara_isolar = dirs["path"] / "bin" / "catamara-isolar"
    modelo_esforco = f"{autor_modelo}-{autor_esforco}"
    cmd_interno = [
        str(catamara_isolar),
        "--conta",
        conta,
        "--perfil",
        "agente",
        "--",
        "agy",
        "--model",
        modelo_esforco,
        "--effort",
        autor_esforco,
    ]
    cmd_str = shlex.join(cmd_interno)
    dados_sessao["comando"] = cmd_str

    if dry_run or sem_tmux:
        return dados_sessao

    tmux_cmd = [
        "tmux",
        "-L",
        "catamara",
        "new-session",
        "-d",
        "-s",
        tmux_nome,
        "-c",
        str(caminho_worktree),
        "-e",
        f"CATAMARA_CONTA={conta}",
        "-e",
        f"CATAMARA_SESSAO={sessao_id}",
        cmd_str,
    ]
    proc_tmux = subprocess.run(tmux_cmd, capture_output=True, text=True, check=False)
    if proc_tmux.returncode != 0:
        raise RuntimeError(f"Erro ao iniciar sessao no tmux: {proc_tmux.stderr.strip()}")

    return dados_sessao


def subcomando_iniciar(args: argparse.Namespace) -> int:
    repo_arg = args.repo or args.projeto_dir or "."
    try:
        dados_sessao = criar_e_iniciar_sessao(
            repo=repo_arg,
            sessao_id=args.sessao,
            conta=args.conta,
            direto=args.direto,
            forcar=args.forcar,
            dry_run=args.dry_run,
            sem_tmux=getattr(args, "sem_tmux", False),
        )
    except Exception as exc:
        print(f"Erro: {exc}", file=sys.stderr)
        return 1

    if args.dry_run or getattr(args, "sem_tmux", False):
        if args.formato_json:
            print(json.dumps(dados_sessao, ensure_ascii=False, indent=2))
        else:
            print(f"Sessao preparada: {dados_sessao['sessao']}")
            print(f"Worktree: {dados_sessao['worktree']}")
            print(f"Comando: {dados_sessao.get('comando', '')}")
        return 0

    if args.formato_json:
        print(json.dumps(dados_sessao, ensure_ascii=False, indent=2))
    else:
        print(f"Sessao {dados_sessao['sessao']} iniciada com sucesso (tmux: {dados_sessao['tmux']}).")

    if sys.stdin.isatty() and not args.sem_anexar:
        os.execvp("tmux", ["tmux", "-L", "catamara", "attach-session", "-t", dados_sessao["tmux"]])

    return 0


def subcomando_continuar(args: argparse.Namespace) -> int:
    dirs = obter_diretorios()
    sessao_id = args.sessao
    if not sessao_id and args.sessao_posicional:
        sessao_id = args.sessao_posicional

    if not sessao_id:
        print("Erro: informe a sessao a continuar.", file=sys.stderr)
        return 2

    sessao_json = dirs["sessoes"] / f"{sessao_id}.json"
    if not sessao_json.is_file():
        print(f"Erro: sessao {sessao_id} nao encontrada.", file=sys.stderr)
        return 1

    dados = json.loads(sessao_json.read_text(encoding="utf-8"))
    tmux_nome = dados.get("tmux") or sessao_id

    if tmux_has_session("catamara", tmux_nome):
        if sys.stdin.isatty() and not args.sem_anexar:
            os.execvp("tmux", ["tmux", "-L", "catamara", "attach-session", "-t", tmux_nome])
        else:
            print(f"Sessao {sessao_id} esta ativa no tmux ({tmux_nome}).")
            return 0

    # Recria se nao estiver no tmux
    caminho_worktree = Path(dados.get("worktree", "."))
    conta = dados.get("conta", os.environ.get("CATAMARA_CONTA_PADRAO", "conta1"))
    modelo = dados.get("modelo", "gemini-4-argon")
    esforco = dados.get("esforco", "medium")
    modelo_esforco = f"{modelo}-{esforco}"

    catamara_isolar = dirs["path"] / "bin" / "catamara-isolar"
    cmd_str = shlex.join(
        [
            str(catamara_isolar),
            "--conta",
            conta,
            "--perfil",
            "agente",
            "--",
            "agy",
            "--model",
            modelo_esforco,
            "--effort",
            esforco,
        ]
    )

    tmux_cmd = [
        "tmux",
        "-L",
        "catamara",
        "new-session",
        "-d",
        "-s",
        tmux_nome,
        "-c",
        str(caminho_worktree),
        "-e",
        f"CATAMARA_CONTA={conta}",
        "-e",
        f"CATAMARA_SESSAO={sessao_id}",
        cmd_str,
    ]
    proc_tmux = subprocess.run(tmux_cmd, capture_output=True, text=True, check=False)
    if proc_tmux.returncode != 0:
        print(f"Erro ao recriar sessao no tmux: {proc_tmux.stderr.strip()}", file=sys.stderr)
        return 1

    dados["estado"] = "ativa"
    sessao_json.write_text(
        json.dumps(dados, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    if sys.stdin.isatty() and not args.sem_anexar:
        os.execvp("tmux", ["tmux", "-L", "catamara", "attach-session", "-t", tmux_nome])

    print(f"Sessao {sessao_id} reativada com sucesso.")
    return 0


def encerrar_sessao_por_id(
    sessao_id: str,
    remover_worktree: bool = False,
    forcar: bool = False,
) -> dict[str, Any]:
    dirs = obter_diretorios()
    sessao_json = dirs["sessoes"] / f"{sessao_id}.json"
    dados: dict[str, Any] = {}
    if sessao_json.is_file():
        try:
            dados = json.loads(sessao_json.read_text(encoding="utf-8"))
        except Exception:
            pass

    tmux_nome = dados.get("tmux") or sessao_id

    subprocess.run(
        ["tmux", "-L", "catamara", "kill-session", "-t", tmux_nome],
        capture_output=True,
        check=False,
    )

    dados["estado"] = "encerrada"
    dados["encerrado_em"] = data_iso_atual()

    if sessao_json.is_file():
        sessao_json.write_text(
            json.dumps(dados, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    if remover_worktree:
        caminho_worktree = dados.get("worktree")
        repo_caminho = dados.get("repo")
        if caminho_worktree and repo_caminho:
            wt_path = Path(caminho_worktree)
            if wt_path.exists():
                cmd_rm = ["git", "-C", repo_caminho, "worktree", "remove", str(wt_path)]
                if forcar:
                    cmd_rm.append("--force")
                subprocess.run(cmd_rm, capture_output=True, text=True, check=False)

    return {"ok": True, "sessao": sessao_id, "estado": "encerrada"}


def subcomando_encerrar(args: argparse.Namespace) -> int:
    sessao_id = args.sessao
    if not sessao_id and args.sessao_posicional:
        sessao_id = args.sessao_posicional

    if not sessao_id:
        print("Erro: informe a sessao a encerrar.", file=sys.stderr)
        return 2

    res = encerrar_sessao_por_id(
        sessao_id=sessao_id,
        remover_worktree=args.remover_worktree,
        forcar=args.forcar,
    )

    if args.formato_json:
        print(json.dumps(res, indent=2))
    else:
        print(f"Sessao {sessao_id} encerrada com sucesso.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="sessoes.py",
        description="Gerenciador de sessoes do Catamara.",
        add_help=True,
    )

    subparsers = parser.add_subparsers(dest="subcomando", help="Subcomando")

    # Iniciar / run
    p_iniciar = subparsers.add_parser("iniciar", aliases=["run"], help="Inicia nova sessao")
    p_iniciar.add_argument("projeto_dir", nargs="?", default=None, help="Diretorio do projeto")
    p_iniciar.add_argument("--repo", dest="repo", default=None, help="Caminho do repositorio Git")
    p_iniciar.add_argument("--sessao", dest="sessao", default=None, help="Identificador da sessao")
    p_iniciar.add_argument("--conta", dest="conta", default=None, help="Conta a ser utilizada")
    p_iniciar.add_argument(
        "--direto",
        dest="direto",
        action="store_true",
        help="Usa a raiz do projeto sem criar git worktree",
    )
    p_iniciar.add_argument(
        "--sem-tmux",
        "--dry-run",
        dest="dry_run",
        action="store_true",
        help="Prepara os dados sem disparar a sessao no tmux",
    )
    p_iniciar.add_argument(
        "--sem-anexar",
        dest="sem_anexar",
        action="store_true",
        help="Nao anexa ao tmux apos iniciar",
    )
    p_iniciar.add_argument(
        "--forcar",
        dest="forcar",
        action="store_true",
        help="Sobrescreve configuracao de sessao existente",
    )
    p_iniciar.add_argument(
        "--json",
        dest="formato_json",
        action="store_true",
        help="Saida em formato JSON",
    )

    # Continuar / continue
    p_cont = subparsers.add_parser(
        "continuar", aliases=["continue"], help="Continua sessao existente"
    )
    p_cont.add_argument("sessao_posicional", nargs="?", default=None, help="Identificador da sessao")
    p_cont.add_argument("--sessao", dest="sessao", default=None, help="Identificador da sessao")
    p_cont.add_argument(
        "--sem-anexar",
        dest="sem_anexar",
        action="store_true",
        help="Nao anexa ao tmux",
    )
    p_cont.add_argument(
        "--json",
        dest="formato_json",
        action="store_true",
        help="Saida em formato JSON",
    )

    # Listar / list
    p_list = subparsers.add_parser(
        "listar", aliases=["list"], help="Lista todas as sessoes registradas"
    )
    p_list.add_argument(
        "--json",
        dest="formato_json",
        action="store_true",
        help="Saida em formato JSON",
    )

    # Encerrar / stop
    p_stop = subparsers.add_parser("encerrar", aliases=["stop"], help="Encerra uma sessao")
    p_stop.add_argument("sessao_posicional", nargs="?", default=None, help="Identificador da sessao")
    p_stop.add_argument("--sessao", dest="sessao", default=None, help="Identificador da sessao")
    p_stop.add_argument(
        "--remover-worktree",
        dest="remover_worktree",
        action="store_true",
        help="Remove a pasta git worktree correspondente",
    )
    p_stop.add_argument(
        "--forcar",
        dest="forcar",
        action="store_true",
        help="Forca remocao da worktree",
    )
    p_stop.add_argument(
        "--json",
        dest="formato_json",
        action="store_true",
        help="Saida em formato JSON",
    )

    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else 2
        return code

    if not args.subcomando or args.subcomando in {"listar", "list"}:
        # Se nenhum subcomando foi passado e foi chamado direto, assume listar
        if not hasattr(args, "formato_json"):
            setattr(args, "formato_json", False)
        return subcomando_listar(args)

    if args.subcomando in {"iniciar", "run"}:
        return subcomando_iniciar(args)
    elif args.subcomando in {"continuar", "continue"}:
        return subcomando_continuar(args)
    elif args.subcomando in {"encerrar", "stop"}:
        return subcomando_encerrar(args)

    print(f"Erro: subcomando desconhecido {args.subcomando}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
