#!/usr/bin/env python3
"""Servidor HTTP e coletor do painel ao vivo da Central de Tarefas do Catamara.

Fornece endpoints REST, interface grafica e painel de indicadores conectados aos dados reais:
- orient.py (contexto e git)
- contas.py (status e piscinas de cota)
- sessoes.py (sessoes ativas no tmux e worktrees)
- implementacao.py (entregas delimitadas e manifestos)
- doctor.py (diagnostico do sistema)
- catamara-validar (portao deterministico e revisao cruzada)
- indicadores de economia de cota, balanco de cascos e funil de qualidade
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import fcntl
from http import HTTPStatus
import http.server
import json
import os
from pathlib import Path
import shutil
import socketserver
import subprocess
import sys
import threading
from typing import Any
import urllib.parse

# Garante importacao dos modulos internos do Catamara
RAIZ_CATAMARA = Path(__file__).resolve().parents[2]
if str(RAIZ_CATAMARA) not in sys.path:
    sys.path.insert(0, str(RAIZ_CATAMARA))

from core.contas import contas
from core.delegacao import implementacao
from core.orquestracao import doctor, orient, sessoes


def estado_dir() -> Path:
    return Path(
        os.environ.get(
            "CATAMARA_ESTADO",
            os.path.expanduser("~/.local/state/catamara"),
        )
    )


def checar_trava_local(pasta_estado: Path) -> dict[str, Any]:
    lock_file = pasta_estado / "local.lock"
    if not lock_file.exists():
        return {"ocupada": False, "status": "livre", "arquivo": str(lock_file)}
    try:
        with open(lock_file, "a") as f:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(f, fcntl.LOCK_UN)
            return {"ocupada": False, "status": "livre", "arquivo": str(lock_file)}
    except (BlockingIOError, OSError):
        return {"ocupada": True, "status": "ocupada", "arquivo": str(lock_file)}


def ler_linhas_jsonl_todas(caminho: Path) -> list[dict[str, Any]]:
    if not caminho.is_file():
        return []
    registros: list[dict[str, Any]] = []
    try:
        with open(caminho, "r", encoding="utf-8", errors="replace") as f:
            for linha in f:
                linha = linha.strip()
                if not linha:
                    continue
                try:
                    obj = json.loads(linha)
                    if isinstance(obj, dict):
                        registros.append(obj)
                except Exception:
                    continue
    except Exception:
        pass
    return registros


def ler_ultimas_linhas_jsonl(caminho: Path, limite: int = 10) -> list[dict[str, Any]]:
    todas = ler_linhas_jsonl_todas(caminho)
    return list(reversed(todas[-limite:]))


def coletar_implementacoes(pasta_estado: Path) -> list[dict[str, Any]]:
    pasta_impls = pasta_estado / "implementacoes"
    if not pasta_impls.is_dir():
        return []

    lista: list[dict[str, Any]] = []
    for sub in sorted(pasta_impls.glob("impl-*"), key=lambda p: p.name, reverse=True):
        if not sub.is_dir():
            continue
        manifesto_arq = sub / "manifesto.json"
        entrega_arq = sub / "entrega.json"
        if not manifesto_arq.is_file():
            continue

        try:
            manifesto = json.loads(manifesto_arq.read_text(encoding="utf-8"))
            entrega = None
            if entrega_arq.is_file():
                entrega = json.loads(entrega_arq.read_text(encoding="utf-8"))

            estado_manifesto = manifesto.get("estado", "desconhecido")
            ja_aceito = bool(entrega and entrega.get("aceito") is True)
            pendente = (estado_manifesto == "finalizado") and not ja_aceito

            item = {
                "id": manifesto.get("id") or sub.name,
                "estado": "aceito" if ja_aceito else estado_manifesto,
                "base": manifesto.get("base"),
                "criterio": manifesto.get("criterio"),
                "tarefa": manifesto.get("tarefa"),
                "permitidos": manifesto.get("permitidos", []),
                "novos": manifesto.get("novos", []),
                "repo": manifesto.get("repo"),
                "criado": manifesto.get("criado"),
                "pendente": pendente,
                "entrega": entrega,
            }
            lista.append(item)
        except Exception:
            continue

    return lista


def coletar_sessoes(pasta_estado: Path) -> list[dict[str, Any]]:
    pasta_ses = pasta_estado / "sessoes"
    if not pasta_ses.is_dir():
        return []

    lista: list[dict[str, Any]] = []
    for arq in sorted(pasta_ses.glob("*.json")):
        try:
            dados = json.loads(arq.read_text(encoding="utf-8"))
            if not isinstance(dados, dict):
                continue
            sessao_id = dados.get("sessao") or arq.stem
            tmux_id = dados.get("tmux") or sessao_id

            ativa_no_tmux = sessoes.tmux_has_session("catamara", tmux_id)
            if dados.get("estado") == "ativa" and not ativa_no_tmux:
                dados["estado"] = "inativa"
            elif ativa_no_tmux:
                dados["estado"] = "ativa"

            dados["ativa_no_tmux"] = ativa_no_tmux
            lista.append(dados)
        except Exception:
            continue

    return lista


def calcular_indicadores(
    pasta_estado: Path,
    implementacoes: list[dict[str, Any]],
    contas_dict: dict[str, Any],
) -> dict[str, Any]:
    """Calcula metricas de economia de cota, balanco de cascos e funil de qualidade."""
    todas_delegacoes = ler_linhas_jsonl_todas(pasta_estado / "delegacoes.jsonl")
    todas_validacoes = ler_linhas_jsonl_todas(pasta_estado / "validar.jsonl")
    todos_eventos = ler_linhas_jsonl_todas(pasta_estado / "eventos.jsonl")

    # 1. Economia de cota e roteamento por motor
    total_del = len(todas_delegacoes)
    por_motor = {
        "ollama": {"total": 0, "sucesso": 0, "duracoes": []},
        "agy_flash": {"total": 0, "sucesso": 0, "duracoes": []},
        "implementador": {"total": 0, "sucesso": 0, "duracoes": []},
    }

    for d in todas_delegacoes:
        mot = d.get("motor", "outro")
        pap = d.get("papel", "")
        if pap == "implementador":
            chave = "implementador"
        elif mot == "ollama":
            chave = "ollama"
        else:
            chave = "agy_flash"

        por_motor[chave]["total"] += 1
        if d.get("codigo") == 0:
            por_motor[chave]["sucesso"] += 1
        dur = d.get("duracao_s")
        if isinstance(dur, (int, float)):
            por_motor[chave]["duracoes"].append(dur)

    metricas_motores: dict[str, dict[str, Any]] = {}
    for k, v in por_motor.items():
        media_dur = (sum(v["duracoes"]) / len(v["duracoes"])) if v["duracoes"] else 0.0
        taxa = (v["sucesso"] / v["total"] * 100) if v["total"] > 0 else 100.0
        metricas_motores[k] = {
            "total": v["total"],
            "sucesso": v["sucesso"],
            "taxa_sucesso_pct": round(taxa, 1),
            "duracao_media_s": round(media_dur, 2),
        }

    cota_poupada_pct = round((por_motor["ollama"]["total"] / total_del * 100), 1) if total_del > 0 else 0.0

    # 2. Balanco entre Casco 1 e Casco 2
    c1_total = sum(1 for d in todas_delegacoes if d.get("conta") == "conta1") + sum(1 for v in todas_validacoes if v.get("conta_autor") == "conta1")
    c2_total = sum(1 for d in todas_delegacoes if d.get("conta") == "conta2") + sum(1 for v in todas_validacoes if v.get("conta_revisor") == "conta2")

    c1_resumo = contas_dict.get("conta1", {}).get("resumo_cotas", {})
    c2_resumo = contas_dict.get("conta2", {}).get("resumo_cotas", {})

    balanco_cascos = {
        "conta1_chamadas": c1_total,
        "conta2_chamadas": c2_total,
        "piscinas_comparativo": {
            "argon": {
                "conta1_pct": c1_resumo.get("argon_pct"),
                "conta2_pct": c2_resumo.get("argon_pct"),
            },
            "gemini_flash": {
                "conta1_pct": c1_resumo.get("gemini_pct"),
                "conta2_pct": c2_resumo.get("gemini_pct"),
            },
            "opus_3p": {
                "conta1_pct": c1_resumo.get("3p_pct"),
                "conta2_pct": c2_resumo.get("3p_pct"),
            },
        },
    }

    # 3. Funil de qualidade
    total_val = len(todas_validacoes)
    det_ok = sum(1 for v in todas_validacoes if v.get("deterministico_ok") is True)
    det_taxa = round((det_ok / total_val * 100), 1) if total_val > 0 else 100.0

    rev_com_par = [v for v in todas_validacoes if v.get("conta_revisor")]
    rev_ok = sum(1 for v in rev_com_par if v.get("veredito") == "aprovado")
    rev_taxa = round((rev_ok / len(rev_com_par) * 100), 1) if rev_com_par else 100.0

    impl_aceitas = sum(1 for imp in implementacoes if imp.get("estado") == "aceito")
    impl_pendentes = sum(1 for imp in implementacoes if imp.get("pendente"))

    funil_qualidade = {
        "validacoes_total": total_val,
        "deterministico_ok": det_ok,
        "taxa_deterministico_pct": det_taxa,
        "revisoes_cruzadas_total": len(rev_com_par),
        "revisoes_cruzadas_ok": rev_ok,
        "taxa_revisao_pct": rev_taxa,
        "implementacoes_total": len(implementacoes),
        "implementacoes_aceitas": impl_aceitas,
        "implementacoes_pendentes": impl_pendentes,
    }

    # 4. Linha do tempo unificada (ordenada por ts decrescente)
    eventos_consolidados: list[dict[str, Any]] = []
    for d in todas_delegacoes:
        eventos_consolidados.append({
            "tipo": "delegacao",
            "ts": d.get("ts"),
            "titulo": f"Delegacao {d.get('papel', 'tarefa')} ({d.get('motor', 'desconhecido')})",
            "status": "ok" if d.get("codigo") == 0 else "falha",
            "detalhes": f"Conta: {d.get('conta')} | Modelo: {d.get('modelo')} | Duracao: {d.get('duracao_s')}s",
        })
    for v in todas_validacoes:
        eventos_consolidados.append({
            "tipo": "validacao",
            "ts": v.get("ts"),
            "titulo": f"Validacao Cruzada ({v.get('veredito', 'desconhecido')})",
            "status": "ok" if v.get("veredito") == "aprovado" else "reprovado",
            "detalhes": f"Autor: {v.get('conta_autor')} | Revisor: {v.get('conta_revisor') or 'local'} | Modelo: {v.get('modelo') or 'deterministico'}",
        })
    for e in todos_eventos:
        eventos_consolidados.append({
            "tipo": "evento",
            "ts": e.get("ts"),
            "titulo": e.get("titulo", "Evento do sistema"),
            "status": e.get("status", "info"),
            "detalhes": e.get("detalhe", ""),
        })

    eventos_consolidados.sort(key=lambda x: str(x.get("ts") or ""), reverse=True)

    return {
        "economia_motores": {
            "total_delegacoes": total_del,
            "cota_poupada_pct": cota_poupada_pct,
            "por_motor": metricas_motores,
        },
        "balanco_cascos": balanco_cascos,
        "funil_qualidade": funil_qualidade,
        "linha_do_tempo": eventos_consolidados[:15],
    }


def coletar_estado_painel(repo_caminho: Path | None = None) -> dict[str, Any]:
    """Coleta o estado unificado e real do Catamara."""
    pasta_estado = estado_dir()
    repo_alvo = repo_caminho or RAIZ_CATAMARA

    orient_dados = orient.coletar_orient(repo_alvo)

    contas_lista: list[dict[str, Any]] = []
    contas_dict: dict[str, dict[str, Any]] = {}
    for nome_conta in contas.contas_configuradas():
        st = contas.status_conta(nome_conta, consultar_remoto=False)
        contas_lista.append(st)
        contas_dict[nome_conta] = st

    sessoes_lista = coletar_sessoes(pasta_estado)
    implementacoes_lista = coletar_implementacoes(pasta_estado)
    pendentes_lista = [imp for imp in implementacoes_lista if imp.get("pendente")]

    trava_local = checar_trava_local(pasta_estado)
    recibos_validar = ler_ultimas_linhas_jsonl(pasta_estado / "validar.jsonl", limite=5)
    recibos_delegacoes = ler_ultimas_linhas_jsonl(pasta_estado / "delegacoes.jsonl", limite=5)

    ultimo_val = recibos_validar[0] if recibos_validar else None
    ultimo_val_ok = bool(ultimo_val and (ultimo_val.get("codigo") == 0 or ultimo_val.get("veredito") == "aprovado"))

    c1_resumo = contas_dict.get("conta1", {}).get("resumo_cotas", {})
    c2_resumo = contas_dict.get("conta2", {}).get("resumo_cotas", {})

    c1_argon = c1_resumo.get("argon_pct")
    c2_flash = c2_resumo.get("gemini_pct")

    # Calculo dos indicadores
    indicadores = calcular_indicadores(pasta_estado, implementacoes_lista, contas_dict)

    # Resumo pronto para Waybar e cabecalhos
    resumo_barra = {
        "sessoes_ativas": sum(1 for s in sessoes_lista if s.get("estado") == "ativa"),
        "sessoes_total": len(sessoes_lista),
        "entregas_pendentes": len(pendentes_lista),
        "portao_ok": ultimo_val_ok,
        "vram_livre_mib": orient_dados.get("ollama", {}).get("vram_livre_mib"),
        "c1_argon_pct": c1_argon,
        "c2_flash_pct": c2_flash,
        "trava_ocupada": trava_local.get("ocupada", False),
    }

    return {
        "versao": 1,
        "gerado_em": datetime.now(timezone.utc).isoformat(),
        "repo": orient_dados.get("repo", {}),
        "contas": contas_lista,
        "contas_map": contas_dict,
        "ollama": orient_dados.get("ollama", {}),
        "sessoes": sessoes_lista,
        "implementacoes": implementacoes_lista,
        "implementacoes_pendentes": pendentes_lista,
        "trava_local": trava_local,
        "historico": {
            "validar": recibos_validar,
            "delegacoes": recibos_delegacoes,
        },
        "indicadores": indicadores,
        "resumo_barra": resumo_barra,
    }


class RequisicaoPainelHandler(http.server.SimpleHTTPRequestHandler):
    """Handler HTTP com endpoints REST para o painel."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.diretorio_painel = Path(__file__).resolve().parent
        super().__init__(*args, directory=str(self.diretorio_painel), **kwargs)

    def log_message(self, format: str, *args: Any) -> None:
        # Silencia logs no terminal para manter saida limpa
        pass

    def _enviar_json(self, dados: Any, status: int = 200) -> None:
        conteudo = json.dumps(dados, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(conteudo)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.end_headers()
        self.wfile.write(conteudo)

    def _enviar_erro(self, mensagem: str, status: int = 400) -> None:
        self._enviar_json({"ok": False, "erro": mensagem}, status=status)

    def _ler_corpo_json(self) -> dict[str, Any]:
        try:
            tam = int(self.headers.get("Content-Length", 0))
            if tam <= 0:
                return {}
            corpo = self.rfile.read(tam).decode("utf-8")
            return json.loads(corpo) if corpo else {}
        except Exception as exc:
            raise ValueError(f"JSON invalido no corpo da requisicao: {exc}")

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        rota = parsed.path

        if rota in {"/", "/index.html", "/central"}:
            central_html = self.diretorio_painel / "central.html"
            if not central_html.is_file():
                central_html = self.diretorio_painel / "mockup_central.html"
            if central_html.is_file():
                conteudo = central_html.read_bytes()
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(conteudo)))
                self.end_headers()
                self.wfile.write(conteudo)
                return

        if rota in {"/api/estado", "/api/snapshot"}:
            try:
                estado = coletar_estado_painel()
                self._enviar_json(estado)
            except Exception as exc:
                self._enviar_erro(f"falha ao coletar estado: {exc}", status=500)
            return

        if rota == "/api/indicadores":
            try:
                estado = coletar_estado_painel()
                self._enviar_json(estado.get("indicadores", {}))
            except Exception as exc:
                self._enviar_erro(f"falha ao coletar indicadores: {exc}", status=500)
            return

        if rota == "/api/health":
            self._enviar_json({"status": "ok", "app": "catamara"})
            return

        super().do_GET()

    def do_POST(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        rota = parsed.path

        if rota == "/api/atualizar-cotas":
            try:
                # Consulta forcadamente as cotas ao vivo
                for c in contas.contas_configuradas():
                    contas.consultar_cotas_conta(c, forcar_atualizacao=True)
                estado = coletar_estado_painel()
                self._enviar_json({"ok": True, "estado": estado})
            except Exception as exc:
                self._enviar_erro(f"falha ao atualizar cotas: {exc}", status=500)
            return

        if rota == "/api/implementacao/aceitar":
            try:
                dados = self._ler_corpo_json()
                impl_id = dados.get("id")
                entrega_hash = dados.get("entrega_hash")
                repo_str = dados.get("repo")
                if not impl_id or not entrega_hash:
                    self._enviar_erro("campos 'id' e 'entrega_hash' sao obrigatorios", status=400)
                    return
                repo_path = Path(repo_str) if repo_str else None
                recibo = implementacao.aceitar(impl_id, entrega_hash, repo_path)
                self._enviar_json({"ok": True, "recibo": recibo})
            except Exception as exc:
                self._enviar_erro(f"falha ao aceitar implementacao: {exc}", status=400)
            return

        if rota == "/api/validar":
            try:
                dados = self._ler_corpo_json()
                so_det = bool(dados.get("so_deterministico", True))
                cmd = [str(RAIZ_CATAMARA / "bin" / "catamara-validar"), "--json"]
                if so_det:
                    cmd.append("--so-deterministico")
                proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
                saida_json = None
                try:
                    saida_json = json.loads(proc.stdout)
                except Exception:
                    saida_json = {"saida_bruta": proc.stdout}
                self._enviar_json({
                    "ok": proc.returncode == 0,
                    "codigo": proc.returncode,
                    "resultado": saida_json,
                    "erro": proc.stderr,
                })
            except Exception as exc:
                self._enviar_erro(f"falha ao validar: {exc}", status=500)
            return

        if rota == "/api/doctor":
            try:
                diag = doctor.executar_doctor(rapido=True)
                self._enviar_json({"ok": True, "diagnostico": diag})
            except Exception as exc:
                self._enviar_erro(f"falha ao executar doctor: {exc}", status=500)
            return

        self._enviar_erro(f"endpoint POST desconhecido: {rota}", status=404)


def iniciar_servidor(porta: int = 8770, abrir_navegador: bool = False) -> None:
    # Vincula estritamente a 127.0.0.1 para seguranca
    endereco = ("127.0.0.1", porta)
    try:
        with socketserver.TCPServer(endereco, RequisicaoPainelHandler) as httpd:
            url = f"http://127.0.0.1:{porta}"
            print(f"Central de Tarefas do Catamara ativa em {url}")
            if abrir_navegador:
                subprocess.Popen(["xdg-open", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                httpd.serve_forever()
            except KeyboardInterrupt:
                print("\nEncerrando servidor...")
    except OSError as err:
        sys.exit(f"Erro ao iniciar servidor na porta {porta}: {err}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Servidor HTTP da Central de Tarefas do Catamara")
    parser.add_argument("--porta", type=int, default=int(os.environ.get("CATAMARA_PAINEL_PORTA", 8770)), help="Porta TCP local (padrao: 8770)")
    parser.add_argument("--snapshot-json", action="store_true", help="Imprime o estado atual em JSON na stdout e sai")
    parser.add_argument("--abrir", "--web", dest="abrir", action="store_true", help="Abre o navegador padrao no endereco da central")
    args = parser.parse_args()

    if args.snapshot_json:
        estado = coletar_estado_painel()
        print(json.dumps(estado, ensure_ascii=False, indent=2))
        return 0

    iniciar_servidor(porta=args.porta, abrir_navegador=args.abrir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
