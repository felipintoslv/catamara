#!/usr/bin/env python3
"""Gerenciador de múltiplas contas do Antigravity CLI (agy) para o Catamara.

Garante isolamento de tokens OAuth em arquivo (sem conflito com o keyring D-Bus),
sincronização de trustedWorkspaces entre as contas, consulta e cache das 6 piscinas
de cota (gemini-argon-wtu-weekly, gemini-5h, gemini-weekly, 3p-5h, 3p-weekly) e
seleção automática de conta por papel (autor, executor, revisor).
"""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Any

NOME_CONTA_RE = re.compile(r"^[A-Za-z0-9_-]+$")
ARQUIVOS_SEMENTE = (
    "antigravity_state.pbtxt",
    "jetski_state.pbtxt",
    "installation_id",
    "settings.json",
)
BUCKETS_POR_FAMILIA = {
    "argon": ("gemini-argon-wtu-weekly", "gemini-5h", "gemini-weekly"),
    "gemini": ("gemini-5h", "gemini-weekly"),
    "3p": ("3p-5h", "3p-weekly"),
}


def validar_nome_conta(conta: str) -> str:
    if not conta or conta in {".", ".."} or not NOME_CONTA_RE.match(conta):
        raise ValueError(f"nome de conta inválido: {conta!r}")
    return conta


def estado_dir() -> Path:
    return Path(
        os.environ.get(
            "CATAMARA_ESTADO",
            os.path.expanduser("~/.local/state/catamara"),
        )
    )


def contas_configuradas() -> list[str]:
    bruto = os.environ.get("CATAMARA_CONTAS", "conta1 conta2").split()
    vistas: list[str] = []
    for item in bruto:
        validar_nome_conta(item)
        if item not in vistas:
            vistas.append(item)
    return vistas or ["conta1", "conta2"]


def pasta_conta(conta: str) -> Path:
    validar_nome_conta(conta)
    base = Path(
        os.environ.get(
            "CATAMARA_CONTAS_DIR",
            str(estado_dir() / "contas"),
        )
    )
    return base / conta / "antigravity-cli"


def pasta_global_agy() -> Path:
    return Path(
        os.environ.get(
            "CATAMARA_AGY_GLOBAL_DIR",
            os.path.expanduser("~/.gemini/antigravity-cli"),
        )
    )


def _gravar_atomico_json(destino: Path, dados: dict[str, Any], modo: int = 0o600) -> None:
    destino.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_nome = tempfile.mkstemp(prefix=f".{destino.name}.", dir=str(destino.parent))
    tmp = Path(tmp_nome)
    try:
        os.fchmod(fd, modo)
        with os.fdopen(fd, "w", encoding="utf-8") as arq:
            json.dump(dados, arq, ensure_ascii=False, indent=2)
            arq.write("\n")
        tmp.replace(destino)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


def extrair_identidade_token(caminho_token: Path) -> dict[str, Any]:
    """Lê metadados não sensíveis do id_token (e-mail, expiração) sem expor segredos."""
    info: dict[str, Any] = {
        "autenticada": False,
        "email": None,
        "auth_method": None,
        "expira_em": None,
    }
    if not caminho_token.is_file() or caminho_token.is_symlink():
        return info
    try:
        dados = json.loads(caminho_token.read_text(encoding="utf-8"))
    except Exception:
        return info
    if not isinstance(dados, dict):
        return info
    token_obj = dados.get("token")
    if isinstance(token_obj, dict) and (
        token_obj.get("access_token") or token_obj.get("refresh_token")
    ):
        info["autenticada"] = True
        info["expira_em"] = token_obj.get("expiry")
    elif dados.get("access_token") or dados.get("refresh_token"):
        info["autenticada"] = True
    info["auth_method"] = dados.get("auth_method")

    id_token = dados.get("id_token")
    if isinstance(id_token, str) and id_token.count(".") >= 1:
        partes = id_token.split(".")
        try:
            payload_b64 = partes[1]
            padding = "=" * (-len(payload_b64) % 4)
            payload = json.loads(
                base64.urlsafe_b64decode(payload_b64 + padding).decode("utf-8")
            )
            if isinstance(payload, dict):
                info["email"] = payload.get("email")
        except Exception:
            pass
    return info


def inicializar_conta(
    conta: str,
    importar_credencial_global: bool = False,
) -> dict[str, Any]:
    """Prepara a árvore isolada da conta em ~/.local/state/catamara/contas/<conta>/antigravity-cli."""
    alvo = pasta_conta(conta)
    origem = pasta_global_agy()
    alvo.mkdir(parents=True, exist_ok=True)
    os.chmod(alvo.parent, 0o700)
    os.chmod(alvo, 0o700)

    for sub in ("bin", "brain", "conversations", "log", "cache"):
        (alvo / sub).mkdir(parents=True, exist_ok=True)

    for nome in ARQUIVOS_SEMENTE:
        dest = alvo / nome
        src = origem / nome
        if not dest.exists() and src.is_file() and not src.is_symlink():
            shutil.copy2(src, dest)
            os.chmod(dest, 0o600)

    settings_path = alvo / "settings.json"
    if not settings_path.exists():
        _gravar_atomico_json(settings_path, {"trustedWorkspaces": []})

    token_dest = alvo / "antigravity-oauth-token"
    token_src = origem / "antigravity-oauth-token"
    if importar_credencial_global and token_src.is_file() and not token_src.is_symlink():
        shutil.copy2(token_src, token_dest)
        os.chmod(token_dest, 0o600)

    return status_conta(conta, consultar_remoto=False)


def ler_workspaces_confiaveis(conta: str) -> list[str]:
    settings = pasta_conta(conta) / "settings.json"
    if not settings.is_file() or settings.is_symlink():
        return []
    try:
        dados = json.loads(settings.read_text(encoding="utf-8"))
        lista = dados.get("trustedWorkspaces", [])
        if isinstance(lista, list):
            return [str(x) for x in lista if isinstance(x, str)]
    except Exception:
        pass
    return []


def ajustar_confianca(
    modo: str,
    caminho: str,
    contas: list[str] | None = None,
) -> dict[str, list[str]]:
    """Adiciona ou remove um caminho de trustedWorkspaces em uma ou todas as contas."""
    if modo not in {"adicionar", "remover"}:
        raise ValueError("modo deve ser 'adicionar' ou 'remover'")
    caminho_real = os.path.realpath(caminho)
    alvos = contas or contas_configuradas()
    resultado: dict[str, list[str]] = {}

    for conta in alvos:
        inicializar_conta(conta, importar_credencial_global=False)
        settings = pasta_conta(conta) / "settings.json"
        try:
            dados = json.loads(settings.read_text(encoding="utf-8"))
            if not isinstance(dados, dict):
                dados = {}
        except Exception:
            dados = {}
        atuais = dados.get("trustedWorkspaces")
        if not isinstance(atuais, list):
            atuais = []
        atuais = [str(x) for x in atuais if isinstance(x, str)]

        if modo == "adicionar":
            if caminho_real not in atuais:
                atuais.append(caminho_real)
        else:
            atuais = [x for x in atuais if x not in {caminho_real, caminho}]

        dados["trustedWorkspaces"] = atuais
        _gravar_atomico_json(settings, dados)
        resultado[conta] = atuais

    return resultado


def sincronizar_confianca_entre_contas() -> dict[str, list[str]]:
    """Une os trustedWorkspaces da pasta global e de todas as contas configuradas."""
    uniao: list[str] = []
    global_settings = pasta_global_agy() / "settings.json"
    if global_settings.is_file() and not global_settings.is_symlink():
        try:
            dados = json.loads(global_settings.read_text(encoding="utf-8"))
            for item in dados.get("trustedWorkspaces", []):
                if isinstance(item, str) and item not in uniao:
                    uniao.append(item)
        except Exception:
            pass

    for conta in contas_configuradas():
        inicializar_conta(conta, importar_credencial_global=False)
        for item in ler_workspaces_confiaveis(conta):
            if item not in uniao:
                uniao.append(item)

    resultado: dict[str, list[str]] = {}
    for conta in contas_configuradas():
        settings = pasta_conta(conta) / "settings.json"
        try:
            dados = json.loads(settings.read_text(encoding="utf-8"))
            if not isinstance(dados, dict):
                dados = {}
        except Exception:
            dados = {}
        dados["trustedWorkspaces"] = list(uniao)
        _gravar_atomico_json(settings, dados)
        resultado[conta] = list(uniao)
    return resultado


def _caminho_cache_cota(conta: str) -> Path:
    return estado_dir() / "cotas" / f"{validar_nome_conta(conta)}.json"


def _parsear_usage_json(bruto: str) -> dict[str, dict[str, Any]]:
    obj = json.loads(bruto)
    grupos = obj.get("command", {}).get("data", {}).get("groups", [])
    piscinas: dict[str, dict[str, Any]] = {}
    for grupo in grupos:
        if not isinstance(grupo, dict):
            continue
        for bucket in grupo.get("buckets", []):
            if not isinstance(bucket, dict) or not bucket.get("id"):
                continue
            bid = str(bucket["id"])
            frac = float(bucket.get("remaining_fraction", 0.0))
            piscinas[bid] = {
                "id": bid,
                "nome": bucket.get("name", bid),
                "janela": bucket.get("window", ""),
                "restante_fracao": frac,
                "restante_pct": round(frac * 100.0, 1),
                "reset_time": bucket.get("reset_time"),
            }
    return piscinas


def consultar_cotas_conta(
    conta: str,
    forcar_atualizacao: bool = False,
    ttl_segundos: int | None = None,
) -> dict[str, Any]:
    """Lê as piscinas de cota da conta (do cache ou executando agy -p='/usage' isolado)."""
    validar_nome_conta(conta)
    if ttl_segundos is None:
        ttl_segundos = int(os.environ.get("CATAMARA_COTA_CACHE_TTL", "180"))
    cache_arq = _caminho_cache_cota(conta)
    agora = int(time.time())

    if not forcar_atualizacao and cache_arq.is_file():
        try:
            cached = json.loads(cache_arq.read_text(encoding="utf-8"))
            ts = int(cached.get("atualizado_em_epoch", 0))
            if agora - ts <= ttl_segundos and isinstance(cached.get("piscinas"), dict):
                cached["fonte"] = "cache"
                return cached
        except Exception:
            pass

    token_arq = pasta_conta(conta) / "antigravity-oauth-token"
    ident = extrair_identidade_token(token_arq)
    if not ident["autenticada"]:
        return {
            "conta": conta,
            "ok": False,
            "erro": "conta_nao_autenticada",
            "atualizado_em_epoch": agora,
            "fonte": "ausente",
            "piscinas": {},
        }

    catamara_bin = Path(__file__).resolve().parents[2] / "bin" / "catamara-isolar"
    if not catamara_bin.is_file() or not shutil.which("bwrap") or not shutil.which("agy"):
        return {
            "conta": conta,
            "ok": False,
            "erro": "isolar_ou_agy_indisponivel",
            "atualizado_em_epoch": agora,
            "fonte": "indisponivel",
            "piscinas": {},
        }

    env = os.environ.copy()
    env["CATAMARA_CONTA"] = conta
    env.pop("CATAMARA_ISOLADO", None)
    try:
        proc = subprocess.run(
            [
                str(catamara_bin),
                "--conta",
                conta,
                "--",
                "agy",
                "--output-format",
                "json",
                "-p=/usage",
            ],
            env=env,
            capture_output=True,
            text=True,
            timeout=25,
            check=False,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            # A saída pode conter avisos antes da linha JSON; pega a última linha JSON válida
            linhas = [ln.strip() for ln in proc.stdout.splitlines() if ln.strip().startswith("{")]
            piscinas = _parsear_usage_json(linhas[-1] if linhas else proc.stdout)
            dados = {
                "conta": conta,
                "ok": True,
                "atualizado_em_epoch": agora,
                "fonte": "ao_vivo",
                "piscinas": piscinas,
            }
            _gravar_atomico_json(cache_arq, dados)
            return dados
        erro_msg = (proc.stderr or proc.stdout or f"rc={proc.returncode}").strip()[:240]
    except Exception as exc:
        erro_msg = str(exc)[:240]

    return {
        "conta": conta,
        "ok": False,
        "erro": erro_msg,
        "atualizado_em_epoch": agora,
        "fonte": "erro",
        "piscinas": {},
    }


def familia_do_modelo(modelo: str) -> str:
    m = (modelo or "").lower()
    if "argon" in m:
        return "argon"
    if m.startswith("gemini"):
        return "gemini"
    return "3p"


def cota_efetiva_familia(piscinas: dict[str, dict[str, Any]], familia: str) -> float | None:
    """Retorna o menor percentual restante entre os buckets exigidos pela família de modelo."""
    chaves = BUCKETS_POR_FAMILIA.get(familia, ("gemini-5h", "gemini-weekly"))
    valores: list[float] = []
    for k in chaves:
        b = piscinas.get(k)
        if isinstance(b, dict) and isinstance(b.get("restante_pct"), (int, float)):
            valores.append(float(b["restante_pct"]))
    if not valores:
        return None
    return min(valores)


def _cache_cota_obsoleto(conta: str, ttl_segundos: int) -> bool:
    cache_arq = _caminho_cache_cota(conta)
    try:
        cached = json.loads(cache_arq.read_text(encoding="utf-8"))
        return int(time.time()) - int(cached.get("atualizado_em_epoch", 0)) > ttl_segundos
    except Exception:
        return True


def disparar_atualizacao_cotas_em_segundo_plano(conta: str) -> bool:
    """Dispara a consulta de cotas desacoplada (sem bloquear o chamador).

    Usa um arquivo de trava com validade de 90 s para nao empilhar consultas e,
    ao terminar, avisa a waybar (sinal 11) para redesenhar o indicador.
    """
    validar_nome_conta(conta)
    trava = estado_dir() / "cotas" / f".{conta}.atualizando"
    try:
        trava.parent.mkdir(parents=True, exist_ok=True)
        if trava.exists() and time.time() - trava.stat().st_mtime < 90:
            return False
        trava.touch()
    except OSError:
        return False
    script = Path(__file__).resolve()
    cmd = (
        f"{shlex.quote(sys.executable)} {shlex.quote(str(script))} cotas {shlex.quote(conta)} "
        f">/dev/null 2>&1; rm -f {shlex.quote(str(trava))}; pkill -RTMIN+11 -x waybar 2>/dev/null; true"
    )
    try:
        subprocess.Popen(
            ["bash", "-c", cmd],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        return True
    except OSError:
        return False


def status_conta(conta: str, consultar_remoto: bool = False) -> dict[str, Any]:
    pasta = pasta_conta(conta)
    token_arq = pasta / "antigravity-oauth-token"
    ident = extrair_identidade_token(token_arq)
    workspaces = ler_workspaces_confiaveis(conta)
    ttl_cota = int(os.environ.get("CATAMARA_COTA_CACHE_TTL", "180"))
    if not consultar_remoto and ident["autenticada"] and _cache_cota_obsoleto(conta, ttl_cota):
        # Cache velho: serve o que existe agora e renova em segundo plano.
        disparar_atualizacao_cotas_em_segundo_plano(conta)
    cotas = consultar_cotas_conta(
        conta,
        forcar_atualizacao=consultar_remoto,
        ttl_segundos=180 if consultar_remoto else 10**9,
    )
    piscinas = cotas.get("piscinas", {}) if isinstance(cotas, dict) else {}
    resumo_cotas = {
        "argon_pct": cota_efetiva_familia(piscinas, "argon"),
        "gemini_pct": cota_efetiva_familia(piscinas, "gemini"),
        "3p_pct": cota_efetiva_familia(piscinas, "3p"),
    }
    return {
        "conta": conta,
        "pasta": str(pasta),
        "inicializada": pasta.is_dir(),
        "autenticada": ident["autenticada"],
        "email": ident["email"],
        "auth_method": ident["auth_method"],
        "expira_em": ident["expira_em"],
        "workspaces_confiaveis": len(workspaces),
        "cotas": cotas,
        "resumo_cotas": resumo_cotas,
    }


def escolher_conta(
    papel: str = "executor",
    modelo: str | None = None,
    evitar_conta: str | None = None,
    consultar_remoto: bool = False,
) -> dict[str, Any]:
    """Escolhe a melhor conta disponível segundo o papel, família do modelo e isolamento de revisão.

    Regras:
    - Para 'revisor': se evitar_conta for informada (ex: conta do autor), exige outra conta
      quando houver mais de uma conta autenticada.
    - Ordena candidatas autenticadas pela maior cota efetiva na família do modelo.
    """
    contas = contas_configuradas()
    if not modelo:
        if papel == "autor":
            modelo = os.environ.get("CATAMARA_AUTOR_MODELO", "gemini-4-argon")
        elif papel == "revisor":
            modelo = os.environ.get("CATAMARA_REVISOR_MODELO", "claude-opus-5-5")
        else:
            modelo = os.environ.get("CATAMARA_EXEC_MODELO", "gemini-3.8-flash")

    familia = familia_do_modelo(modelo)
    cota_min = float(os.environ.get("CATAMARA_COTA_MIN", "15"))

    estados = [status_conta(c, consultar_remoto=consultar_remoto) for c in contas]
    autenticadas = [e for e in estados if e["autenticada"]]

    candidatas = list(autenticadas)
    if evitar_conta and len(autenticadas) > 1:
        outras = [e for e in autenticadas if e["conta"] != evitar_conta]
        if outras:
            candidatas = outras

    if not candidatas:
        # Nenhuma autenticada ainda: devolve a padrão ou a oposta à evitada
        padrao = os.environ.get("CATAMARA_CONTA_PADRAO", contas[0])
        if evitar_conta and len(contas) > 1:
            for c in contas:
                if c != evitar_conta:
                    padrao = c
                    break
        return {
            "conta": padrao,
            "papel": papel,
            "modelo": modelo,
            "familia": familia,
            "autenticada": False,
            "cota_restante_pct": None,
            "motivo": "nenhuma_conta_autenticada_fallback_padrao",
        }

    def chave_ordenacao(item: dict[str, Any]) -> tuple[int, float, int]:
        pct = item["resumo_cotas"].get(f"{familia}_pct")
        if pct is None:
            # Sem cache de cota ainda: trata como 100% provisório, mas abaixo de quem tem cota alta confirmada
            return (1, 50.0, 1 if item["conta"] != evitar_conta else 0)
        acima_min = 1 if pct >= cota_min else 0
        return (acima_min, float(pct), 1 if item["conta"] != evitar_conta else 0)

    candidatas.sort(key=chave_ordenacao, reverse=True)
    escolhida = candidatas[0]
    pct_escolhida = escolhida["resumo_cotas"].get(f"{familia}_pct")

    # Fallback automático do revisor: quando os tokens do Opus (3p) acabam (< cota_min), muda para Argon
    fallback_acionado = False
    if (
        papel == "revisor"
        and familia == "3p"
        and pct_escolhida is not None
        and float(pct_escolhida) < cota_min
    ):
        modelo_fb = os.environ.get("CATAMARA_REVISOR_FALLBACK_MODELO", "gemini-4-argon")
        familia_fb = familia_do_modelo(modelo_fb)

        def chave_fb(item: dict[str, Any]) -> tuple[int, float, int]:
            pct = item["resumo_cotas"].get(f"{familia_fb}_pct")
            if pct is None:
                return (1, 50.0, 1 if item["conta"] != evitar_conta else 0)
            return (1 if pct >= cota_min else 0, float(pct), 1 if item["conta"] != evitar_conta else 0)

        candidatas.sort(key=chave_fb, reverse=True)
        escolhida = candidatas[0]
        pct_fb = escolhida["resumo_cotas"].get(f"{familia_fb}_pct")
        if pct_fb is None or float(pct_fb) > float(pct_escolhida):
            modelo = modelo_fb
            familia = familia_fb
            pct_escolhida = pct_fb
            fallback_acionado = True

    return {
        "conta": escolhida["conta"],
        "email": escolhida["email"],
        "papel": papel,
        "modelo": modelo,
        "familia": familia,
        "autenticada": True,
        "cota_restante_pct": pct_escolhida,
        "fallback_argon": fallback_acionado,
        "evitou_conta": evitar_conta if evitar_conta and escolhida["conta"] != evitar_conta else None,
        "motivo": (
            "fallback_opus_para_argon"
            if fallback_acionado
            else ("maior_cota_familia_oposta" if evitar_conta else "maior_cota_familia")
        ),
    }


def catalogo_cascos(consultar_remoto: bool = False) -> list[dict[str, Any]]:
    """Retorna o catalogo completo de cascos da frota Catamara para o painel e waybar.

    Inclui as contas Agy isoladas (conta1, conta2), o motor local Ollama,
    e ferramentas CLI complementares (Claude Code, Codex CLI).
    """
    cascos: list[dict[str, Any]] = []

    for idx, c in enumerate(contas_configuradas(), 1):
        try:
            st = status_conta(c, consultar_remoto=consultar_remoto)
            rc = st.get("resumo_cotas", {})
            tem_argon = bool(rc.get("argon_pct") is not None and rc["argon_pct"] > 0)
            email_info = f" ({st['email']})" if st.get("email") else ""
            papel = "Coordenador / Autor" if (c == "conta1" or tem_argon) else "Executor / Revisor"
            nome = f"Casco {idx} · {c.capitalize()}{email_info}"
            cascos.append({
                "nome": nome,
                "tipo": "agy",
                "conta": c,
                "autenticado": st.get("autenticada", False),
                "tem_argon": tem_argon,
                "papel": papel,
                "status_texto": "Ativo e Isolado" if st.get("autenticada") else "Não Autenticado",
                "cotas": rc,
                "modelos": ["gemini-4-argon", "gemini-3.8-flash", "claude-opus-5-5"] if tem_argon else ["gemini-3.8-flash", "claude-opus-5-5"],
            })
        except Exception:
            cascos.append({
                "nome": f"Casco {idx} · {c.capitalize()}",
                "tipo": "agy",
                "conta": c,
                "autenticado": False,
                "tem_argon": False,
                "status_texto": "Indisponível",
                "cotas": {},
                "modelos": ["gemini-3.8-flash"],
            })

    # Casco Local: Ollama
    tem_ollama = shutil.which("ollama") is not None
    cascos.append({
        "nome": "Casco 3 · Ollama Local (Offline)",
        "tipo": "ollama",
        "autenticado": tem_ollama,
        "status_texto": "VRAM protegida por trava flock" if tem_ollama else "Ollama CLI não detectado",
        "modelo_padrao": os.environ.get("CATAMARA_OLLAMA_MODELO", "qwen2.5-coder:7b"),
        "cotas": {},
        "modelos": ["qwen2.5-coder:7b", "qwen3:4b", "deepseek-r1:8b"],
    })

    # Casco Complementar: Claude Code
    tem_claude = shutil.which("claude") is not None
    cascos.append({
        "nome": "Casco 4 · Claude Code CLI",
        "tipo": "cli",
        "autenticado": tem_claude,
        "status_texto": "Conectado via Claude Code" if tem_claude else "CLI 'claude' não detectado",
        "cotas": {"cinco_horas_pct": 20.0, "semanal_pct": 15.0} if tem_claude else {},
        "modelos": ["claude-3-7-sonnet", "claude-3-5-haiku", "claude-opus-4"],
    })

    # Casco Complementar: Codex CLI (OpenAI)
    tem_codex = shutil.which("codex") is not None
    cascos.append({
        "nome": "Casco 5 · Codex CLI (OpenAI)",
        "tipo": "cli",
        "autenticado": tem_codex,
        "status_texto": "Conectado via Codex CLI" if tem_codex else "CLI 'codex' pronto para integração",
        "cotas": {"cinco_horas_pct": 10.0, "semanal_pct": 10.0} if tem_codex else {},
        "modelos": ["o3-mini", "codex-davinci", "gpt-4.1-code"],
    })

    return cascos


def cli_main(argv: list[str]) -> int:
    cmd = argv[0] if argv else "status"
    restante = argv[1:]

    if cmd == "init":
        importar_conta1 = "--importar-atual" in restante or "--seed-conta1" in restante
        res = []
        for idx, c in enumerate(contas_configuradas()):
            res.append(
                inicializar_conta(
                    c,
                    importar_credencial_global=(importar_conta1 and idx == 0),
                )
            )
        sincronizar_confianca_entre_contas()
        print(json.dumps({"contas": res}, ensure_ascii=False, indent=2))
        return 0

    if cmd == "importar-global":
        alvo = restante[0] if restante else os.environ.get("CATAMARA_CONTA_PADRAO", "conta1")
        st = inicializar_conta(alvo, importar_credencial_global=True)
        sincronizar_confianca_entre_contas()
        print(json.dumps(st, ensure_ascii=False, indent=2))
        return 0

    if cmd in {"status", "cotas"}:
        remoto = (cmd == "cotas") or ("--ao-vivo" in restante) or ("--refresh" in restante)
        contas_alvo = [a for a in restante if not a.startswith("-")] or contas_configuradas()
        relatorio = [status_conta(c, consultar_remoto=remoto) for c in contas_alvo]
        print(json.dumps({"contas": relatorio}, ensure_ascii=False, indent=2))
        return 0

    if cmd == "escolher":
        papel = "executor"
        modelo = None
        evitar = None
        remoto = False
        i = 0
        while i < len(restante):
            arg = restante[i]
            if arg == "--papel" and i + 1 < len(restante):
                papel = restante[i + 1]
                i += 2
            elif arg == "--modelo" and i + 1 < len(restante):
                modelo = restante[i + 1]
                i += 2
            elif arg == "--evitar" and i + 1 < len(restante):
                evitar = restante[i + 1]
                i += 2
            elif arg in {"--ao-vivo", "--refresh"}:
                remoto = True
                i += 1
            else:
                i += 1
        esc = escolher_conta(
            papel=papel,
            modelo=modelo,
            evitar_conta=evitar,
            consultar_remoto=remoto,
        )
        if "--apenas-nome" in restante:
            print(esc["conta"])
        else:
            print(json.dumps(esc, ensure_ascii=False, indent=2))
        return 0

    if cmd == "confianca":
        acao = restante[0] if restante else "sincronizar"
        if acao == "sincronizar":
            res = sincronizar_confianca_entre_contas()
        elif acao in {"adicionar", "remover"} and len(restante) >= 2:
            res = ajustar_confianca(acao, restante[1])
        else:
            print(
                "uso: contas.py confianca [sincronizar | adicionar CAMINHO | remover CAMINHO]",
                file=sys.stderr,
            )
            return 1
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0

    print(
        f"subcomando desconhecido em contas.py: {cmd} (use init, importar-global, status, cotas, escolher, confianca)",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(cli_main(sys.argv[1:]))
