#!/usr/bin/env python3
"""Ciclo de implementacao delimitada: preparacao, isolamento, finalizacao e aceite."""

from __future__ import annotations

import argparse
import datetime
import difflib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
from typing import Any

ID_IMPL_RE = re.compile(r"^impl-\d{14}-[0-9a-f]{6}$")


def estado_dir() -> Path:
    return Path(
        os.environ.get(
            "CATAMARA_ESTADO",
            os.path.expanduser("~/.local/state/catamara"),
        )
    )


def gerar_id_implementacao() -> str:
    agora = datetime.datetime.now(datetime.timezone.utc)
    carimbo = agora.strftime("%Y%m%d%H%M%S")
    sufixo = secrets.token_hex(3)
    return f"impl-{carimbo}-{sufixo}"


def data_iso_utc() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def calcular_sha256_bytes(dados: bytes) -> str:
    return hashlib.sha256(dados).hexdigest()


def calcular_sha256_arquivo(caminho: Path) -> str:
    h = hashlib.sha256()
    with open(caminho, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


def json_canonico_bytes(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def validar_caminho_relativo(caminho_str: str, raiz: Path | None = None) -> str:
    """Valida se o caminho eh relativo seguro, sem .., .git ou symlink fora da raiz."""
    if not isinstance(caminho_str, str) or not caminho_str.strip():
        raise ValueError(f"caminho vazio: {caminho_str!r}")
    caminho_str = caminho_str.strip()
    if os.path.isabs(caminho_str):
        raise ValueError(f"caminho absoluto proibido: {caminho_str}")

    partes = Path(caminho_str).parts
    if any(p in {".", "..", ".git"} for p in partes):
        raise ValueError(f"caminho com travessia ou reservado: {caminho_str}")

    if raiz is not None:
        alvo = (raiz / caminho_str).resolve()
        raiz_real = raiz.resolve()
        try:
            alvo.relative_to(raiz_real)
        except ValueError:
            raise ValueError(f"caminho escapa da raiz: {caminho_str}")

    return caminho_str


def obter_raiz_repo(caminho: Path | None = None) -> Path:
    base = caminho or Path.cwd()
    cmd = ["git", "-C", str(base), "rev-parse", "--show-toplevel"]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        raise ValueError(f"nao eh repositorio git valido: {base}")
    return Path(res.stdout.strip()).resolve()


def preparar(
    base: str,
    arquivos: list[str],
    permitidos: list[str],
    novos: list[str] | None,
    criterio: str,
    tarefa: str,
    repo_caminho: Path | None = None,
) -> dict[str, Any]:
    repo = obter_raiz_repo(repo_caminho)

    # Validar formato do SHA da base
    if not re.fullmatch(r"^[0-9a-fA-F]{7,64}$", base):
        raise ValueError(f"SHA base invalido: {base}")

    # Normalizar e validar caminhos
    novos_set = {validar_caminho_relativo(n, repo) for n in (novos or [])}
    permitidos_set = {validar_caminho_relativo(p, repo) for p in permitidos}
    arquivos_set = {validar_caminho_relativo(a, repo) for a in arquivos}

    # Novos sao permitidos e arquivos
    permitidos_set.update(novos_set)
    arquivos_set.update(permitidos_set)

    # Permitidos precisa estar contido em arquivos
    if not permitidos_set.issubset(arquivos_set):
        diferenca = permitidos_set - arquivos_set
        raise ValueError(f"permitidos nao estao em arquivos: {diferenca}")

    # Cada item de novos nao pode existir em base
    for n in novos_set:
        check = subprocess.run(
            ["git", "-C", str(repo), "cat-file", "-e", f"{base}:{n}"],
            capture_output=True,
        )
        if check.returncode == 0:
            raise ValueError(f"arquivo novo ja existe na base git: {n}")

    impl_id = gerar_id_implementacao()
    pasta_impl = estado_dir() / "implementacoes" / impl_id
    workspace = pasta_impl / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)

    hashes_iniciais: dict[str, str] = {}
    vazio_sha = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"

    for arq in sorted(arquivos_set):
        destino = workspace / arq
        destino.parent.mkdir(parents=True, exist_ok=True)

        if arq in novos_set:
            destino.touch(mode=0o600)
            hashes_iniciais[arq] = vazio_sha
        else:
            show = subprocess.run(
                ["git", "-C", str(repo), "show", f"{base}:{arq}"],
                capture_output=True,
                check=False,
            )
            if show.returncode != 0:
                raise ValueError(f"falha ao extrair {arq} da base git {base}")
            destino.write_bytes(show.stdout)
            os.chmod(destino, 0o600)
            hashes_iniciais[arq] = calcular_sha256_bytes(show.stdout)

    manifesto: dict[str, Any] = {
        "id": impl_id,
        "repo": str(repo),
        "base": base,
        "criado": data_iso_utc(),
        "tarefa": tarefa,
        "criterio": criterio,
        "arquivos": hashes_iniciais,
        "permitidos": sorted(permitidos_set),
        "novos": sorted(novos_set),
        "estado": "preparado",
    }

    manifesto_path = pasta_impl / "manifesto.json"
    manifesto_path.write_text(json.dumps(manifesto, ensure_ascii=False, indent=2), encoding="utf-8")

    return {
        "id": impl_id,
        "manifesto": str(manifesto_path),
        "workspace": str(workspace),
        "base": base,
    }


def montagem(manifesto_caminho: str, pasta_conta: str) -> list[str]:
    p_man = Path(manifesto_caminho).resolve()
    if not p_man.is_file():
        raise ValueError(f"manifesto nao encontrado: {manifesto_caminho}")

    dados = json.loads(p_man.read_text(encoding="utf-8"))
    workspace = p_man.parent / "workspace"
    if not workspace.is_dir():
        raise ValueError(f"workspace nao encontrado: {workspace}")

    pasta_conta_path = Path(pasta_conta).resolve()
    home_cli = Path.home() / ".gemini" / "antigravity-cli"

    caminhos_para_verificar = [
        str(workspace),
        str(pasta_conta_path),
        str(home_cli),
    ]
    for p in dados.get("permitidos", []):
        caminhos_para_verificar.append(str(workspace / p))

    for c in caminhos_para_verificar:
        if "\n" in c or "\r" in c:
            raise ValueError(f"caminho com quebra de linha: {c!r}")

    args: list[str] = [
        "--ro-bind",
        str(workspace),
        str(workspace),
    ]

    for p in sorted(dados.get("permitidos", [])):
        alvo = str(workspace / p)
        args.extend(["--bind", alvo, alvo])

    args.extend([
        "--bind",
        str(pasta_conta_path),
        str(home_cli),
        "--chdir",
        str(workspace),
    ])

    return args


def finalizar(impl_id: str) -> dict[str, Any]:
    if not ID_IMPL_RE.match(impl_id):
        raise ValueError(f"id de implementacao invalido: {impl_id}")

    pasta_impl = estado_dir() / "implementacoes" / impl_id
    manifesto_path = pasta_impl / "manifesto.json"
    if not manifesto_path.is_file():
        raise ValueError(f"manifesto nao encontrado para id {impl_id}")

    manifesto = json.loads(manifesto_path.read_text(encoding="utf-8"))
    workspace = pasta_impl / "workspace"
    if not workspace.is_dir():
        raise ValueError(f"workspace nao encontrado para id {impl_id}")

    arquivos_esperados = set(manifesto["arquivos"].keys())
    permitidos = set(manifesto.get("permitidos", []))
    novos = set(manifesto.get("novos", []))
    vazio_sha = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"

    # Inspeciona todos os arquivos no workspace
    arquivos_no_disco: dict[str, str] = {}
    for raiz, _, files in os.walk(workspace):
        for f in files:
            p = Path(raiz) / f
            if p.is_symlink():
                # Rejeita symlinks que escapem do workspace
                try:
                    p.resolve().relative_to(workspace.resolve())
                except ValueError:
                    manifesto["estado"] = "rejeitado"
                    manifesto_path.write_text(json.dumps(manifesto, ensure_ascii=False, indent=2))
                    raise RuntimeError(f"symlink escapando do workspace: {p}")
            rel = str(p.relative_to(workspace))
            arquivos_no_disco[rel] = calcular_sha256_arquivo(p)

    # 1. Confere se algum arquivo original foi removido
    removidos = arquivos_esperados - set(arquivos_no_disco.keys())
    if removidos:
        manifesto["estado"] = "rejeitado"
        manifesto_path.write_text(json.dumps(manifesto, ensure_ascii=False, indent=2))
        raise RuntimeError(f"arquivos esperados foram removidos: {removidos}")

    # 2. Confere se arquivos extras foram criados fora do contrato
    extras = set(arquivos_no_disco.keys()) - arquivos_esperados
    if extras:
        manifesto["estado"] = "rejeitado"
        manifesto_path.write_text(json.dumps(manifesto, ensure_ascii=False, indent=2))
        raise RuntimeError(f"arquivos extras nao autorizados no workspace: {extras}")

    # 3. Confere mudancas fora de permitidos
    alterados: dict[str, dict[str, str | None]] = {}
    diffs_texto: list[str] = []

    for arq in sorted(arquivos_esperados):
        sha_antes = manifesto["arquivos"][arq]
        sha_depois = arquivos_no_disco[arq]

        if sha_depois != sha_antes:
            if arq not in permitidos:
                manifesto["estado"] = "rejeitado"
                manifesto_path.write_text(json.dumps(manifesto, ensure_ascii=False, indent=2))
                raise RuntimeError(f"arquivo alterado fora de permitidos: {arq}")

            if arq in novos and sha_depois == vazio_sha:
                # Novo que continua vazio nao entra na entrega
                continue

            antes_val = None if (arq in novos and sha_antes == vazio_sha) else sha_antes
            alterados[arq] = {
                "antes": antes_val,
                "depois": sha_depois,
            }

            # Gera diff
            caminho_disco = workspace / arq
            conteudo_depois = caminho_disco.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)

            if antes_val is None:
                conteudo_antes: list[str] = []
            else:
                # Recupera conteudo original da base
                repo = Path(manifesto["repo"])
                show = subprocess.run(
                    ["git", "-C", str(repo), "show", f"{manifesto['base']}:{arq}"],
                    capture_output=True,
                    text=True,
                    errors="replace",
                )
                conteudo_antes = show.stdout.splitlines(keepends=True) if show.returncode == 0 else []

            diff_linhas = list(
                difflib.unified_diff(
                    conteudo_antes,
                    conteudo_depois,
                    fromfile=f"a/{arq}",
                    tofile=f"b/{arq}",
                )
            )
            if diff_linhas:
                diffs_texto.append("".join(diff_linhas))

    diff_completo = "".join(diffs_texto)
    entrega_dados: dict[str, Any] = {
        "id": impl_id,
        "base": manifesto["base"],
        "alterados": alterados,
        "diff": diff_completo,
        "criado": data_iso_utc(),
    }

    # Hash canonico da entrega
    entrega_hash = hashlib.sha256(json_canonico_bytes(entrega_dados)).hexdigest()
    entrega_dados["entrega_hash"] = entrega_hash

    entrega_path = pasta_impl / "entrega.json"
    entrega_path.write_text(json.dumps(entrega_dados, ensure_ascii=False, indent=2), encoding="utf-8")

    manifesto["estado"] = "finalizado"
    manifesto_path.write_text(json.dumps(manifesto, ensure_ascii=False, indent=2), encoding="utf-8")

    return entrega_dados


def aceitar(impl_id: str, entrega_hash: str, repo_caminho: Path | None = None) -> dict[str, Any]:
    if not ID_IMPL_RE.match(impl_id):
        raise ValueError(f"id de implementacao invalido: {impl_id}")

    pasta_impl = estado_dir() / "implementacoes" / impl_id
    manifesto_path = pasta_impl / "manifesto.json"
    entrega_path = pasta_impl / "entrega.json"
    workspace = pasta_impl / "workspace"

    if not manifesto_path.is_file() or not entrega_path.is_file() or not workspace.is_dir():
        raise RuntimeError("arquivos da implementacao incompletos para aceite")

    manifesto = json.loads(manifesto_path.read_text(encoding="utf-8"))
    entrega = json.loads(entrega_path.read_text(encoding="utf-8"))

    if entrega.get("aceito") is True:
        raise RuntimeError("esta entrega ja foi aceita anteriormente")

    # Confere entrega_hash
    dados_para_hash = {k: v for k, v in entrega.items() if k != "entrega_hash"}
    hash_recalculado = hashlib.sha256(json_canonico_bytes(dados_para_hash)).hexdigest()
    if hash_recalculado != entrega_hash or entrega.get("entrega_hash") != entrega_hash:
        raise RuntimeError(f"entrega_hash diverge do registrado: {hash_recalculado} != {entrega_hash}")

    repo = obter_raiz_repo(repo_caminho or Path(manifesto["repo"]))

    # Confere repositorio
    head_check = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
    )
    head_sha = head_check.stdout.strip() if head_check.returncode == 0 else ""

    alterados = entrega.get("alterados", {})
    # Se HEAD for igual a base, perfeito. Senao, confere arquivo por arquivo
    if head_sha != manifesto["base"]:
        for arq, info in alterados.items():
            alvo = repo / arq
            antes_sha = info.get("antes")
            if antes_sha is None:
                if alvo.exists():
                    raise RuntimeError(f"arquivo novo da entrega ja existe no repo: {arq}")
            else:
                if not alvo.is_file():
                    raise RuntimeError(f"arquivo alvo nao existe no repo: {arq}")
                if calcular_sha256_arquivo(alvo) != antes_sha:
                    raise RuntimeError(f"conteudo atual de {arq} no repo difere do estado inicial na base")

    # Aplicacao atomica com backup
    backup_dir = pasta_impl / "backup"
    backup_dir.mkdir(parents=True, exist_ok=True)

    arquivos_modificados: list[tuple[Path, Path]] = []
    try:
        for arq in alterados.keys():
            origem = workspace / arq
            destino = repo / arq
            destino.parent.mkdir(parents=True, exist_ok=True)

            if destino.exists():
                copia_bkp = backup_dir / arq
                copia_bkp.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(destino, copia_bkp)

            # Grava temporario no mesmo diretorio de destino com fsync
            fd, tmp_nome = tempfile.mkstemp(prefix=f".{destino.name}.", dir=str(destino.parent))
            try:
                dados_origem = origem.read_bytes()
                with os.fdopen(fd, "wb") as f_tmp:
                    f_tmp.write(dados_origem)
                    f_tmp.flush()
                    os.fsync(f_tmp.fileno())
                arquivos_modificados.append((Path(tmp_nome), destino))
            except Exception:
                Path(tmp_nome).unlink(missing_ok=True)
                raise

        # os.replace sequencial atomico
        for tmp_arq, dest_arq in arquivos_modificados:
            tmp_arq.replace(dest_arq)

    except Exception as exc:
        # Rollback dos arquivos a partir do backup
        for arq in alterados.keys():
            bkp = backup_dir / arq
            dest = repo / arq
            if bkp.exists():
                shutil.copy2(bkp, dest)
            elif dest.exists() and alterados[arq].get("antes") is None:
                dest.unlink(missing_ok=True)
        raise RuntimeError(f"falha durante aplicacao atomica, rollback executado: {exc}") from exc

    # Atualiza entrega e manifesto
    entrega["aceito"] = True
    entrega["aceito_em"] = data_iso_utc()
    entrega_path.write_text(json.dumps(entrega, ensure_ascii=False, indent=2), encoding="utf-8")

    manifesto["estado"] = "aceito"
    manifesto_path.write_text(json.dumps(manifesto, ensure_ascii=False, indent=2), encoding="utf-8")

    return {
        "id": impl_id,
        "aceito": True,
        "entrega_hash": entrega_hash,
    }


def rejeitar(impl_id: str, motivo: str = "rejeitado pelo coordenador") -> dict[str, Any]:
    if not ID_IMPL_RE.match(impl_id):
        raise ValueError(f"id de implementacao invalido: {impl_id}")

    pasta_impl = estado_dir() / "implementacoes" / impl_id
    manifesto_path = pasta_impl / "manifesto.json"
    entrega_path = pasta_impl / "entrega.json"

    if not manifesto_path.is_file():
        raise RuntimeError("manifesto nao encontrado para rejeicao")

    manifesto = json.loads(manifesto_path.read_text(encoding="utf-8"))
    manifesto["estado"] = "rejeitado"
    manifesto["motivo_rejeicao"] = motivo
    manifesto_path.write_text(json.dumps(manifesto, ensure_ascii=False, indent=2), encoding="utf-8")

    entrega: dict[str, Any] = {}
    if entrega_path.is_file():
        try:
            entrega = json.loads(entrega_path.read_text(encoding="utf-8"))
        except Exception:
            pass
    entrega["aceito"] = False
    entrega["rejeitado"] = True
    entrega["motivo_rejeicao"] = motivo
    entrega["rejeitado_em"] = data_iso_utc()
    entrega_path.write_text(json.dumps(entrega, ensure_ascii=False, indent=2), encoding="utf-8")

    return {
        "id": impl_id,
        "estado": "rejeitado",
        "motivo": motivo,
        "rejeitado_em": entrega["rejeitado_em"],
    }


def main(argv: list[str] | None = None) -> int:
    args_list = sys.argv[1:] if argv is None else argv
    if not args_list:
        print("uso: implementacao.py [preparar | montagem | finalizar | aceitar] ...", file=sys.stderr)
        return 2

    subcmd = args_list[0]
    restante = args_list[1:]

    if subcmd == "preparar":
        parser = argparse.ArgumentParser(prog="implementacao.py preparar")
        parser.add_argument("--base", required=True)
        parser.add_argument("--arquivos", action="append", default=[], required=True)
        parser.add_argument("--permitidos", action="append", default=[], required=True)
        parser.add_argument("--novos", action="append", default=[])
        parser.add_argument("--criterio", required=True)
        parser.add_argument("--tarefa", required=True)
        parser.add_argument("--repo")

        try:
            parsed = parser.parse_args(restante)
            # Suporta virgulas e espacos
            def desmembrar(lista: list[str]) -> list[str]:
                out: list[str] = []
                for item in lista:
                    for sub in item.replace(",", " ").split():
                        if sub.strip():
                            out.append(sub.strip())
                return out

            arqs = desmembrar(parsed.arquivos)
            perms = desmembrar(parsed.permitidos)
            novs = desmembrar(parsed.novos)

            res = preparar(
                base=parsed.base,
                arquivos=arqs,
                permitidos=perms,
                novos=novs,
                criterio=parsed.criterio,
                tarefa=parsed.tarefa,
                repo_caminho=Path(parsed.repo) if parsed.repo else None,
            )
            print(json.dumps(res, ensure_ascii=False))
            return 0
        except ValueError as exc_val:
            print(f"erro nos argumentos de preparar: {exc_val}", file=sys.stderr)
            return 2
        except Exception as exc_gen:
            print(f"falha em preparar: {exc_gen}", file=sys.stderr)
            return 1

    if subcmd == "montagem":
        if len(restante) < 2:
            print("uso: implementacao.py montagem MANIFESTO_JSON PASTA_CONTA", file=sys.stderr)
            return 2
        try:
            tokens = montagem(restante[0], restante[1])
            for t in tokens:
                print(t)
            return 0
        except ValueError as exc_val:
            print(f"erro na montagem: {exc_val}", file=sys.stderr)
            return 2
        except Exception as exc_gen:
            print(f"falha na montagem: {exc_gen}", file=sys.stderr)
            return 1

    if subcmd == "finalizar":
        # Suporta tanto "finalizar ID" quanto "finalizar --id ID"
        parser = argparse.ArgumentParser(prog="implementacao.py finalizar")
        parser.add_argument("pos_id", nargs="?")
        parser.add_argument("--id", dest="flag_id")

        try:
            parsed = parser.parse_args(restante)
            impl_id = parsed.flag_id or parsed.pos_id
            if not impl_id:
                print("erro: id de implementacao obrigatorio", file=sys.stderr)
                return 2
            res = finalizar(impl_id)
            print(json.dumps(res, ensure_ascii=False))
            return 0
        except ValueError as exc_val:
            print(f"erro nos argumentos de finalizar: {exc_val}", file=sys.stderr)
            return 2
        except Exception as exc_gen:
            print(f"falha em finalizar: {exc_gen}", file=sys.stderr)
            return 1

    if subcmd == "aceitar":
        parser = argparse.ArgumentParser(prog="implementacao.py aceitar")
        parser.add_argument("--id", required=True)
        parser.add_argument("--entrega", required=True)
        parser.add_argument("--repo")

        try:
            parsed = parser.parse_args(restante)
            res = aceitar(
                impl_id=parsed.id,
                entrega_hash=parsed.entrega,
                repo_caminho=Path(parsed.repo) if parsed.repo else None,
            )
            print(json.dumps(res, ensure_ascii=False))
            return 0
        except ValueError as exc_val:
            print(f"erro nos argumentos de aceitar: {exc_val}", file=sys.stderr)
            return 2
        except Exception as exc_gen:
            print(f"falha em aceitar: {exc_gen}", file=sys.stderr)
            return 1

    print(f"subcomando desconhecido: {subcmd}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
