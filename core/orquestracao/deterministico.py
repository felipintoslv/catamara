#!/usr/bin/env python3
"""Verificador deterministico local de codigo para o Catamara.

Executa verificacoes locais e deterministicas sobre arquivos rastreados ou alterados:
1. Marcadores de conflito git (<<<<<<<, =======, >>>>>>>);
2. Sintaxe de scripts shell com bash -n;
3. Compilacao de arquivos Python com compile() em memoria (sem gravar pycache);
4. Varredura de segredos e credenciais sensiveis (tokens OAuth, chaves privadas);
5. Execucao opcional de comando de teste com timeout.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
from typing import Any

# Padroes de segredos construidos sem literais continuos para evitar falso positivo em si mesmo
PADROES_SEGREDOS: list[tuple[str, re.Pattern[str]]] = [
    ("google_oauth", re.compile("ya" + r"29\.[\w-]{20,}")),
    ("google_refresh", re.compile("1//0" + r"[\w-]{20,}")),
    ("google_api_key", re.compile("AI" + r"za[\w-]{35}")),
    ("anthropic_key", re.compile("sk-ant-" + r"[\w-]{20,}")),
    ("github_token", re.compile("ghp_" + r"\w{36}")),
    ("private_key", re.compile("-----" + r"BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")),
    (
        "json_token",
        re.compile(r'"(?:refresh_token|client_secret)"' + r'\s*:\s*"[^"]{8,}"'),
    ),
]


def data_iso_atual() -> str:
    return datetime.now(timezone.utc).isoformat()


def mascarar_segredo(texto: str) -> str:
    if len(texto) <= 8:
        return "***"
    return f"{texto[:4]}...{texto[-4:]}"


def obter_raiz_repo(diretorio: Path) -> Path:
    try:
        proc = subprocess.run(
            ["git", "-C", str(diretorio), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            return Path(proc.stdout.strip()).resolve()
    except Exception:
        pass
    return diretorio.resolve()


def listar_arquivos_alvo(repo_dir: Path, base_ref: str | None = None) -> list[str]:
    """Lista arquivos relativos ao repositorio para inspecao."""
    arquivos: set[str] = set()
    eh_git = (repo_dir / ".git").exists()

    if eh_git:
        if base_ref:
            try:
                proc = subprocess.run(
                    [
                        "git",
                        "-C",
                        str(repo_dir),
                        "diff",
                        "--name-only",
                        "--diff-filter=ACMR",
                        base_ref,
                    ],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                if proc.returncode == 0:
                    for lin in proc.stdout.splitlines():
                        c = lin.strip()
                        if c:
                            arquivos.add(c)
            except Exception:
                pass
        else:
            try:
                proc = subprocess.run(
                    ["git", "-C", str(repo_dir), "ls-files"],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                if proc.returncode == 0:
                    for lin in proc.stdout.splitlines():
                        c = lin.strip()
                        if c:
                            arquivos.add(c)
            except Exception:
                pass

        # Sempre inclui arquivos nao rastreados (outros) respeitando exclude-standard
        try:
            proc_outros = subprocess.run(
                ["git", "-C", str(repo_dir), "ls-files", "--others", "--exclude-standard"],
                capture_output=True,
                text=True,
                check=False,
            )
            if proc_outros.returncode == 0:
                for lin in proc_outros.stdout.splitlines():
                    c = lin.strip()
                    if c:
                        arquivos.add(c)
        except Exception:
            pass
    else:
        # Varredura em diretorio comum sem git
        for raiz, dirs, files in os.walk(repo_dir):
            dirs[:] = [d for d in dirs if not d.startswith(".") and d != "__pycache__"]
            for f in files:
                if f.startswith("."):
                    continue
                caminho_abs = Path(raiz) / f
                try:
                    rel = caminho_abs.relative_to(repo_dir)
                    arquivos.add(str(rel))
                except ValueError:
                    pass

    # Filtra apenas arquivos existentes, nao ignorados e nao binarios obvios
    extensoes_binarias = {".pyc", ".pyo", ".pyd", ".so", ".png", ".jpg", ".jpeg", ".ico", ".bin"}
    resultado: list[str] = []
    for rel in sorted(arquivos):
        if "__pycache__" in rel or "/." in rel or rel.startswith("."):
            continue
        abs_p = repo_dir / rel
        if abs_p.is_file() and not abs_p.is_symlink():
            if abs_p.suffix.lower() in extensoes_binarias:
                continue
            resultado.append(rel)
    return resultado


def eh_script_shell(abs_path: Path, rel_str: str) -> bool:
    if rel_str.endswith(".sh") or rel_str.endswith(".bash"):
        return True
    try:
        with open(abs_path, "rb") as f:
            primeira_linha = f.readline(256)
            if primeira_linha.startswith(b"#!"):
                if b"sh" in primeira_linha or b"bash" in primeira_linha:
                    return True
    except Exception:
        pass
    return False


def eh_script_python(abs_path: Path, rel_str: str) -> bool:
    if rel_str.endswith(".py"):
        return True
    try:
        with open(abs_path, "rb") as f:
            primeira_linha = f.readline(256)
            if primeira_linha.startswith(b"#!") and b"python" in primeira_linha:
                return True
    except Exception:
        pass
    return False


def verificar_conflitos(repo_dir: Path, arquivos: list[str]) -> tuple[bool, list[str]]:
    detalhes: list[str] = []
    marcador_abertura = "<" * 7 + " "
    marcador_fechamento = ">" * 7 + " "
    marcador_divisor = "=" * 7

    for rel in arquivos:
        abs_path = repo_dir / rel
        try:
            with open(abs_path, "r", encoding="utf-8", errors="replace") as f:
                dentro_conflito = False
                for idx, linha in enumerate(f, start=1):
                    if linha.startswith(marcador_abertura):
                        dentro_conflito = True
                        detalhes.append(f"{rel}:{idx}: marcador de inicio de conflito ({linha.strip()})")
                    elif dentro_conflito and linha.strip() == marcador_divisor:
                        detalhes.append(f"{rel}:{idx}: marcador divisor de conflito ({linha.strip()})")
                    elif linha.startswith(marcador_fechamento):
                        dentro_conflito = False
                        detalhes.append(f"{rel}:{idx}: marcador de fim de conflito ({linha.strip()})")
        except Exception as exc:
            detalhes.append(f"{rel}: erro de leitura ao verificar conflitos: {exc}")

    ok = len(detalhes) == 0
    return ok, detalhes


def verificar_bash(repo_dir: Path, arquivos: list[str]) -> tuple[bool, list[str]]:
    detalhes: list[str] = []
    scripts = [rel for rel in arquivos if eh_script_shell(repo_dir / rel, rel)]

    for rel in scripts:
        abs_path = repo_dir / rel
        try:
            proc = subprocess.run(
                ["bash", "-n", str(abs_path)],
                capture_output=True,
                text=True,
                check=False,
            )
            if proc.returncode != 0:
                msg = (proc.stderr or proc.stdout or f"codigo de saida {proc.returncode}").strip()
                detalhes.append(f"{rel}: erro de sintaxe bash: {msg}")
        except Exception as exc:
            detalhes.append(f"{rel}: falha ao executar bash -n: {exc}")

    ok = len(detalhes) == 0
    return ok, detalhes


def verificar_python(repo_dir: Path, arquivos: list[str]) -> tuple[bool, list[str]]:
    detalhes: list[str] = []
    scripts = [rel for rel in arquivos if eh_script_python(repo_dir / rel, rel)]

    for rel in scripts:
        abs_path = repo_dir / rel
        try:
            conteudo = abs_path.read_text(encoding="utf-8")
            compile(conteudo, str(abs_path), "exec")
        except SyntaxError as syn_err:
            detalhes.append(
                f"{rel}:{syn_err.lineno or '?'}: erro de sintaxe python: {syn_err.msg}"
            )
        except Exception as exc:
            detalhes.append(f"{rel}: falha ao compilar python: {exc}")

    ok = len(detalhes) == 0
    return ok, detalhes


def verificar_segredos(repo_dir: Path, arquivos: list[str]) -> tuple[bool, list[str]]:
    detalhes: list[str] = []

    # Se gitleaks estiver instalado e puder ser usado, podemos testar
    gitleaks_bin = shutil.which("gitleaks")
    if gitleaks_bin:
        try:
            proc = subprocess.run(
                [gitleaks_bin, "detect", "--no-git", "--source", str(repo_dir), "--report-format", "json"],
                capture_output=True,
                text=True,
                check=False,
            )
            if proc.returncode != 0 and proc.stdout.strip():
                try:
                    relatorio = json.loads(proc.stdout)
                    if isinstance(relatorio, list):
                        for item in relatorio:
                            arq = item.get("File", "")
                            lin = item.get("StartLine", "")
                            regra = item.get("Description", "segredo detectado por gitleaks")
                            detalhes.append(f"{arq}:{lin}: {regra}")
                        if detalhes:
                            return False, detalhes
                except Exception:
                    pass
        except Exception:
            pass

    # Verificacao nativa por regex
    for rel in arquivos:
        abs_path = repo_dir / rel
        try:
            with open(abs_path, "r", encoding="utf-8", errors="replace") as f:
                for idx, linha in enumerate(f, start=1):
                    for tipo_segredo, padrao in PADROES_SEGREDOS:
                        match = padrao.search(linha)
                        if match:
                            mascarado = mascarar_segredo(match.group(0))
                            detalhes.append(
                                f"{rel}:{idx}: segredo detectado [{tipo_segredo}]: {mascarado}"
                            )
        except Exception as exc:
            detalhes.append(f"{rel}: erro ao analisar segredos: {exc}")

    ok = len(detalhes) == 0
    return ok, detalhes


def verificar_teste(
    repo_dir: Path, comando_teste: str | None, tempo_max: int
) -> tuple[bool, bool, list[str]]:
    if not comando_teste:
        return True, True, []

    detalhes: list[str] = []
    try:
        proc = subprocess.run(
            comando_teste,
            shell=True,
            cwd=str(repo_dir),
            capture_output=True,
            text=True,
            timeout=tempo_max,
            check=False,
        )
        if proc.returncode != 0:
            saida = (proc.stderr or proc.stdout or "").strip()
            resumo_saida = saida.splitlines()[-5:] if saida else []
            detalhes.append(
                f"comando de teste falhou com codigo {proc.returncode}: {' '.join(resumo_saida)}"
            )
            return False, False, detalhes
        return True, False, []
    except subprocess.TimeoutExpired:
        detalhes.append(f"comando de teste excedeu o limite de {tempo_max} segundos")
        return False, False, detalhes
    except Exception as exc:
        detalhes.append(f"falha ao rodar comando de teste: {exc}")
        return False, False, detalhes


def executar_todas_verificacoes(
    repo_caminho: str | None,
    base_ref: str | None,
    comando_teste: str | None,
    tempo_max: int,
) -> dict[str, Any]:
    inicio_geral = time.time()
    caminho_base = Path(repo_caminho).resolve() if repo_caminho else Path.cwd().resolve()
    repo_dir = obter_raiz_repo(caminho_base)
    arquivos = listar_arquivos_alvo(repo_dir, base_ref=base_ref)

    etapas: list[dict[str, Any]] = []

    # 1. Conflitos
    t0 = time.time()
    ok_conf, det_conf = verificar_conflitos(repo_dir, arquivos)
    etapas.append(
        {
            "nome": "conflitos",
            "ok": ok_conf,
            "pulada": False,
            "detalhes": det_conf,
            "duracao_s": round(time.time() - t0, 3),
        }
    )

    # 2. Bash
    t0 = time.time()
    ok_bash, det_bash = verificar_bash(repo_dir, arquivos)
    etapas.append(
        {
            "nome": "bash",
            "ok": ok_bash,
            "pulada": False,
            "detalhes": det_bash,
            "duracao_s": round(time.time() - t0, 3),
        }
    )

    # 3. Python
    t0 = time.time()
    ok_py, det_py = verificar_python(repo_dir, arquivos)
    etapas.append(
        {
            "nome": "python",
            "ok": ok_py,
            "pulada": False,
            "detalhes": det_py,
            "duracao_s": round(time.time() - t0, 3),
        }
    )

    # 4. Segredos
    t0 = time.time()
    ok_seg, det_seg = verificar_segredos(repo_dir, arquivos)
    etapas.append(
        {
            "nome": "segredos",
            "ok": ok_seg,
            "pulada": False,
            "detalhes": det_seg,
            "duracao_s": round(time.time() - t0, 3),
        }
    )

    # 5. Teste
    t0 = time.time()
    ok_tst, pulada_tst, det_tst = verificar_teste(repo_dir, comando_teste, tempo_max)
    etapas.append(
        {
            "nome": "teste",
            "ok": ok_tst,
            "pulada": pulada_tst,
            "detalhes": det_tst,
            "duracao_s": round(time.time() - t0, 3),
        }
    )

    total_ok = sum(1 for e in etapas if e["ok"] and not e["pulada"])
    total_falhas = sum(1 for e in etapas if not e["ok"])
    ok_geral = total_falhas == 0

    return {
        "ok": ok_geral,
        "gerado_em": data_iso_atual(),
        "repo": str(repo_dir),
        "base": base_ref,
        "arquivos": arquivos,
        "etapas": etapas,
        "resumo": {
            "ok": total_ok,
            "falha": total_falhas,
            "total": len(etapas),
            "duracao_total_s": round(time.time() - inicio_geral, 3),
        },
    }


def formatar_texto(resultado: dict[str, Any]) -> str:
    linhas: list[str] = []
    linhas.append(f"Catamara Verificador Deterministico - {resultado['gerado_em']}")
    linhas.append(f"Repositorio: {resultado['repo']}")
    linhas.append(f"Arquivos verificados: {len(resultado.get('arquivos', []))}")
    linhas.append("-" * 60)

    for etapa in resultado.get("etapas", []):
        nome = etapa["nome"].ljust(12)
        if etapa.get("pulada"):
            status = "[PULADA]"
        elif etapa.get("ok"):
            status = "[OK]    "
        else:
            status = "[FALHA] "
        dur = f"({etapa.get('duracao_s', 0.0)}s)"
        linhas.append(f"{status} {nome} {dur}")
        for det in etapa.get("detalhes", []):
            linhas.append(f"  -> {det}")

    linhas.append("-" * 60)
    res = resultado.get("resumo", {})
    veredicto = "SUCESSO" if resultado.get("ok") else "FALHA"
    linhas.append(
        f"Resultado: {veredicto} | Etapas OK: {res.get('ok', 0)} | Falhas: {res.get('falha', 0)}"
    )
    return "\n".join(linhas)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="deterministico.py",
        description="Verificador deterministico local do Catamara.",
        add_help=True,
    )
    parser.add_argument("--repo", dest="repo", default=None, help="Caminho do repositorio")
    parser.add_argument("--base", dest="base", default=None, help="Referencia base do Git")
    parser.add_argument("--teste", dest="teste", default=None, help="Comando opcional de teste")
    parser.add_argument(
        "--tempo-max",
        dest="tempo_max",
        type=int,
        default=600,
        help="Timeout para o comando de teste em segundos",
    )
    parser.add_argument(
        "--json",
        dest="formato_json",
        action="store_true",
        help="Emite relatorio em formato JSON",
    )

    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else 2
        return code

    resultado = executar_todas_verificacoes(
        repo_caminho=args.repo,
        base_ref=args.base,
        comando_teste=args.teste,
        tempo_max=args.tempo_max,
    )

    if args.formato_json:
        print(json.dumps(resultado, ensure_ascii=False, indent=2))
    else:
        print(formatar_texto(resultado))

    return 0 if resultado["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
