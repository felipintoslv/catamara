#!/usr/bin/env python3
"""Central de Operacoes Desktop do Catamara em PyQt6.

Implementa a interface grafica unificada embasada no QWebEngineView (WebEngine)
com a UI oficial da Jangada (central.html), preservando integracao nativa desktop:
1. Container nativo desktop com WebEngine renderizando a Central oficial.
2. Instancia unica via soquete UNIX e foco pelo Hyprland/Wayland.
3. Servidor HTTP local (servidor.py) garantido automaticamente em segundo plano.
4. Fallback transparente para o navegador padrao caso PyQt6-WebEngine nao esteja presente.
5. Suporte a modo nativo legado com widgets puros via flag --nativa.
"""

from __future__ import annotations

import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys
import time
from typing import Any
import webbrowser

# Resolucoes de caminhos do Catamara
RAIZ_CATAMARA = Path(__file__).resolve().parents[2]
if str(RAIZ_CATAMARA) not in sys.path:
    sys.path.insert(0, str(RAIZ_CATAMARA))

from core.contas import contas
from core.delegacao import implementacao
from core.orquestracao import sessoes

try:
    from PyQt6.QtCore import Qt, QTimer, QUrl
    from PyQt6.QtGui import QColor, QFont, QIcon, QKeySequence, QPalette, QPixmap, QShortcut
    from PyQt6.QtNetwork import QLocalServer, QLocalSocket
    from PyQt6.QtWidgets import (
        QApplication,
        QComboBox,
        QDialog,
        QFrame,
        QGroupBox,
        QHBoxLayout,
        QHeaderView,
        QLabel,
        QLineEdit,
        QMainWindow,
        QMessageBox,
        QProgressBar,
        QPushButton,
        QScrollArea,
        QSplitter,
        QTabWidget,
        QTableWidget,
        QTableWidgetItem,
        QTextEdit,
        QVBoxLayout,
        QWidget,
    )
    PYQT_DISPONIVEL = True
except ImportError:
    PYQT_DISPONIVEL = False

try:
    from PyQt6.QtWebEngineWidgets import QWebEngineView
    WEBENGINE_DISPONIVEL = True
except ImportError:
    WEBENGINE_DISPONIVEL = False


# Tokens oficiais da Jangada (fichas.json / modo dark)
COR_SUPERFICIE = "#101417"
COR_ELEVADA = "#1c2024"
COR_ELEVADA_HOVER = "#262b30"
COR_TEXTO = "#e0e3e8"
COR_TEXTO_SUAVE = "#c1c7ce"
COR_BORDA = "rgba(193, 199, 206, 0.18)"
COR_PRIMARIA = "#95cdf7"
COR_TEXTO_PRIMARIO = "#00344e"

COR_EXECUTANDO = "#85baff"
COR_REVISAO = "#c9b6f2"
COR_CONCLUIDA = "#96d6a8"
COR_BLOQUEADA = "#f2c66d"
COR_FALHOU = "#f09d93"


def obter_pasta_estado() -> Path:
    return Path(os.environ.get("CATAMARA_ESTADO", os.path.expanduser("~/.local/state/catamara")))


def caminho_soquete_servidor() -> str:
    runtime = os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    pasta = Path(runtime) / "catamara-tarefas"
    pasta.mkdir(mode=0o700, parents=True, exist_ok=True)
    return str(pasta / "catamara-gui.sock")


if PYQT_DISPONIVEL:

    class CartaoCasco(QFrame):
        """Card de telemetria visual de um casco da frota (AI Usage Bar)."""

        def __init__(self, dados_casco: dict[str, Any], parent: QWidget | None = None) -> None:
            super().__init__(parent)
            self.dados = dados_casco
            self.setObjectName("CartaoCasco")
            self.setStyleSheet(f"""
                #CartaoCasco {{
                    background-color: {COR_ELEVADA};
                    border: 1px solid {COR_BORDA};
                    border-radius: 8px;
                    padding: 8px;
                }}
                #CartaoCasco:hover {{
                    border: 1px solid rgba(149, 205, 247, 0.4);
                }}
            """)
            self.montar_ui()

        def montar_ui(self) -> None:
            layout = QVBoxLayout(self)
            layout.setContentsMargins(10, 8, 10, 8)
            layout.setSpacing(6)

            # Cabecalho do casco: Nome e Badge de tipo
            topo = QHBoxLayout()
            lbl_nome = QLabel(self.dados.get("nome", "Casco"))
            lbl_nome.setStyleSheet(f"font-weight: bold; color: {COR_TEXTO}; font-size: 13px;")
            topo.addWidget(lbl_nome)

            badge_tipo = QLabel(self.dados.get("tipo", "").upper())
            badge_tipo.setStyleSheet(f"""
                background-color: #262b30;
                color: {COR_PRIMARIA};
                border-radius: 4px;
                padding: 2px 6px;
                font-size: 10px;
                font-weight: bold;
            """)
            topo.addWidget(badge_tipo, alignment=Qt.AlignmentFlag.AlignRight)
            layout.addLayout(topo)

            # Barra de cota / uso principal
            cotas = self.dados.get("cotas", {})
            tem_argon = self.dados.get("tem_argon", False)

            if self.dados.get("tipo") == "agy":
                # Indicador fidedigno de Argon
                info_argon = QHBoxLayout()
                lbl_tag_argon = QLabel("Argon (saldo disponível):")
                lbl_tag_argon.setStyleSheet(f"color: {COR_TEXTO_SUAVE}; font-size: 11px;")
                info_argon.addWidget(lbl_tag_argon)

                if tem_argon:
                    pct_argon = cotas.get("argon_pct") or 0
                    lbl_argon_val = QLabel(f"{pct_argon:.1f}%")
                    lbl_argon_val.setStyleSheet(f"color: {COR_CONCLUIDA}; font-weight: bold; font-size: 11px;")
                    info_argon.addWidget(lbl_argon_val, alignment=Qt.AlignmentFlag.AlignRight)
                    layout.addLayout(info_argon)

                    bar_argon = QProgressBar()
                    bar_argon.setRange(0, 100)
                    bar_argon.setValue(int(pct_argon))
                    bar_argon.setTextVisible(False)
                    bar_argon.setFixedHeight(6)
                    bar_argon.setStyleSheet(f"""
                        QProgressBar {{ background-color: #0b0e11; border-radius: 3px; }}
                        QProgressBar::chunk {{ background-color: {COR_CONCLUIDA}; border-radius: 3px; }}
                    """)
                    layout.addWidget(bar_argon)
                else:
                    lbl_sem_argon = QLabel("[Sem Argon: elegível para Flash]")
                    lbl_sem_argon.setStyleSheet(f"color: {COR_TEXTO_SUAVE}; font-style: italic; font-size: 11px;")
                    info_argon.addWidget(lbl_sem_argon, alignment=Qt.AlignmentFlag.AlignRight)
                    layout.addLayout(info_argon)

                # Cota Flash
                info_flash = QHBoxLayout()
                lbl_flash = QLabel("Flash (saldo disponível):")
                lbl_flash.setStyleSheet(f"color: {COR_TEXTO_SUAVE}; font-size: 11px;")
                info_flash.addWidget(lbl_flash)

                pct_flash = cotas.get("gemini_pct") or 0
                lbl_flash_val = QLabel(f"{pct_flash:.1f}%")
                lbl_flash_val.setStyleSheet(f"color: {COR_PRIMARIA}; font-weight: bold; font-size: 11px;")
                info_flash.addWidget(lbl_flash_val, alignment=Qt.AlignmentFlag.AlignRight)
                layout.addLayout(info_flash)

                bar_flash = QProgressBar()
                bar_flash.setRange(0, 100)
                bar_flash.setValue(int(pct_flash))
                bar_flash.setTextVisible(False)
                bar_flash.setFixedHeight(6)
                bar_flash.setStyleSheet(f"""
                    QProgressBar {{ background-color: #0b0e11; border-radius: 3px; }}
                    QProgressBar::chunk {{ background-color: {COR_PRIMARIA}; border-radius: 3px; }}
                """)
                layout.addWidget(bar_flash)

                # Cota Claude / 3P (Claude Opus 5.5 e Sonnet no Agy)
                pct_3p = cotas.get("3p_pct")
                if pct_3p is not None:
                    info_3p = QHBoxLayout()
                    lbl_3p = QLabel("Claude / 3P (saldo disponível):")
                    lbl_3p.setStyleSheet(f"color: {COR_TEXTO_SUAVE}; font-size: 11px;")
                    info_3p.addWidget(lbl_3p)

                    lbl_3p_val = QLabel(f"{float(pct_3p):.1f}%")
                    lbl_3p_val.setStyleSheet(f"color: {COR_REVISAO}; font-weight: bold; font-size: 11px;")
                    info_3p.addWidget(lbl_3p_val, alignment=Qt.AlignmentFlag.AlignRight)
                    layout.addLayout(info_3p)

                    bar_3p = QProgressBar()
                    bar_3p.setRange(0, 100)
                    bar_3p.setValue(int(float(pct_3p)))
                    bar_3p.setTextVisible(False)
                    bar_3p.setFixedHeight(6)
                    bar_3p.setStyleSheet(f"""
                        QProgressBar {{ background-color: #0b0e11; border-radius: 3px; }}
                        QProgressBar::chunk {{ background-color: {COR_REVISAO}; border-radius: 3px; }}
                    """)
                    layout.addWidget(bar_3p)

            elif self.dados.get("tipo") == "ollama":
                lbl_desc = QLabel(f"Motor local {self.dados.get('modelo_padrao', '')}")
                lbl_desc.setStyleSheet(f"color: {COR_TEXTO_SUAVE}; font-size: 11px;")
                layout.addWidget(lbl_desc)

                lbl_vram = QLabel("VRAM protegida por trava flock")
                lbl_vram.setStyleSheet(f"color: {COR_CONCLUIDA}; font-size: 11px;")
                layout.addWidget(lbl_vram)

            elif self.dados.get("tipo") == "cli":
                lbl_st = QLabel(self.dados.get("status_texto", "Ativo"))
                lbl_st.setStyleSheet(f"color: {COR_CONCLUIDA if self.dados.get('autenticado') else COR_BLOQUEADA}; font-size: 11px;")
                layout.addWidget(lbl_st)

                cotas = self.dados.get("cotas", {})
                uso_5h = cotas.get("cinco_horas_pct")
                uso_sem = cotas.get("semanal_pct")

                # Barra Janela 5h
                if uso_5h is not None:
                    info_5h = QHBoxLayout()
                    lbl_5h = QLabel("Uso 5h (consumido):")
                    lbl_5h.setStyleSheet(f"color: {COR_TEXTO_SUAVE}; font-size: 11px;")
                    info_5h.addWidget(lbl_5h)

                    val_5h = float(uso_5h)
                    lbl_5h_val = QLabel(f"{val_5h:.1f}%")
                    cor_5h = COR_CONCLUIDA if val_5h < 70 else (COR_BLOQUEADA if val_5h < 90 else COR_FALHOU)
                    lbl_5h_val.setStyleSheet(f"color: {cor_5h}; font-weight: bold; font-size: 11px;")
                    info_5h.addWidget(lbl_5h_val, alignment=Qt.AlignmentFlag.AlignRight)
                    layout.addLayout(info_5h)

                    bar_5h = QProgressBar()
                    bar_5h.setRange(0, 100)
                    bar_5h.setValue(int(val_5h))
                    bar_5h.setTextVisible(False)
                    bar_5h.setFixedHeight(6)
                    bar_5h.setStyleSheet(f"""
                        QProgressBar {{ background-color: #0b0e11; border-radius: 3px; }}
                        QProgressBar::chunk {{ background-color: {cor_5h}; border-radius: 3px; }}
                    """)
                    layout.addWidget(bar_5h)

                # Barra Semanal
                if uso_sem is not None:
                    info_sem = QHBoxLayout()
                    lbl_sem = QLabel("Uso Semanal (consumido):")
                    lbl_sem.setStyleSheet(f"color: {COR_TEXTO_SUAVE}; font-size: 11px;")
                    info_sem.addWidget(lbl_sem)

                    val_sem = float(uso_sem)
                    lbl_sem_val = QLabel(f"{val_sem:.1f}%")
                    cor_sem = COR_PRIMARIA if val_sem < 70 else (COR_BLOQUEADA if val_sem < 90 else COR_FALHOU)
                    lbl_sem_val.setStyleSheet(f"color: {cor_sem}; font-weight: bold; font-size: 11px;")
                    info_sem.addWidget(lbl_sem_val, alignment=Qt.AlignmentFlag.AlignRight)
                    layout.addLayout(info_sem)

                    bar_sem = QProgressBar()
                    bar_sem.setRange(0, 100)
                    bar_sem.setValue(int(val_sem))
                    bar_sem.setTextVisible(False)
                    bar_sem.setFixedHeight(6)
                    bar_sem.setStyleSheet(f"""
                        QProgressBar {{ background-color: #0b0e11; border-radius: 3px; }}
                        QProgressBar::chunk {{ background-color: {cor_sem}; border-radius: 3px; }}
                    """)
                    layout.addWidget(bar_sem)

                lbl_mods = QLabel("Modelos: " + ", ".join(self.dados.get("modelos", [])))
                lbl_mods.setStyleSheet(f"color: {COR_TEXTO_SUAVE}; font-size: 10px;")
                lbl_mods.setWordWrap(True)
                layout.addWidget(lbl_mods)

            else:
                lbl_st = QLabel(self.dados.get("status_texto", "Ativo"))
                lbl_st.setStyleSheet(f"color: {COR_CONCLUIDA if self.dados.get('autenticado') else COR_BLOQUEADA}; font-size: 11px;")
                layout.addWidget(lbl_st)

                lbl_mods = QLabel("Modelos: " + ", ".join(self.dados.get("modelos", [])))
                lbl_mods.setStyleSheet(f"color: {COR_TEXTO_SUAVE}; font-size: 10px;")
                lbl_mods.setWordWrap(True)
                layout.addWidget(lbl_mods)


    class DialogoNovaConta(QDialog):
        """Dialogo informativo e assistente para adicionar novas contas ou cascos."""

        def __init__(self, parent: QWidget | None = None) -> None:
            super().__init__(parent)
            self.setWindowTitle("Catamarã · Conectar Novo Casco ou Conta")
            self.setMinimumSize(560, 440)
            self.resize(620, 500)
            self.montar_ui()

        def montar_ui(self) -> None:
            layout = QVBoxLayout(self)
            layout.setContentsMargins(18, 18, 18, 18)
            layout.setSpacing(12)

            # Titulo
            lbl_tit = QLabel("Adicionar Nova Conta ou Conectar Casco")
            lbl_tit.setStyleSheet(f"font-size: 16px; font-weight: bold; color: {COR_PRIMARIA};")
            layout.addWidget(lbl_tit)

            lbl_sub = QLabel(
                "O Catamarã opera com uma frota de cascos independentes e isolados. "
                "Escolha o provedor abaixo e siga as instruções para ativação imediata:"
            )
            lbl_sub.setWordWrap(True)
            lbl_sub.setStyleSheet(f"color: {COR_TEXTO_SUAVE}; font-size: 12px;")
            layout.addWidget(lbl_sub)

            # Bloco 1: Antigravity CLI (Agy)
            grp_agy = QGroupBox("1. Nova Conta Antigravity CLI (Agy)")
            grp_agy.setStyleSheet(f"""
                QGroupBox {{
                    color: {COR_TEXTO};
                    font-weight: bold;
                    border: 1px solid {COR_BORDA};
                    border-radius: 6px;
                    margin-top: 8px;
                    padding: 12px;
                    background-color: {COR_ELEVADA};
                }}
            """)
            lay_agy = QVBoxLayout(grp_agy)
            lay_agy.setSpacing(4)

            txt_agy = QLabel(
                "Cada conta Agy roda em sandbox exclusivo (~/.local/state/catamara/contas/<nome>/).\n"
                "Para autenticar uma nova conta (ex: conta3), abra seu terminal e execute:\n"
                "  ./bin/cata conta login conta3\n"
                "Para importar sua credencial ativa atual do host como conta1:\n"
                "  ./bin/cata conta init --importar-atual"
            )
            txt_agy.setStyleSheet("font-family: monospace; font-size: 11px; color: #95cdf7;")
            txt_agy.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            lay_agy.addWidget(txt_agy)
            layout.addWidget(grp_agy)

            # Bloco 2: Claude Code e Codex CLI
            grp_cli = QGroupBox("2. Cascos CLI (Claude Code e OpenAI Codex)")
            grp_cli.setStyleSheet(f"""
                QGroupBox {{
                    color: {COR_TEXTO};
                    font-weight: bold;
                    border: 1px solid {COR_BORDA};
                    border-radius: 6px;
                    margin-top: 8px;
                    padding: 12px;
                    background-color: {COR_ELEVADA};
                }}
            """)
            lay_cli = QVBoxLayout(grp_cli)
            txt_cli = QLabel(
                "• Claude Code: Execute 'claude' e digite '/login' no prompt interativo (ou defina ANTHROPIC_API_KEY).\n"
                "• Codex CLI: Execute 'codex login'. O Catamarã detecta a CLI no seu PATH."
            )
            txt_cli.setStyleSheet("font-family: monospace; font-size: 11px; color: #c9b6f2;")
            txt_cli.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            lay_cli.addWidget(txt_cli)
            layout.addWidget(grp_cli)

            # Bloco 3: Ollama Local
            grp_ol = QGroupBox("3. Motor Local Ollama")
            grp_ol.setStyleSheet(f"""
                QGroupBox {{
                    color: {COR_TEXTO};
                    font-weight: bold;
                    border: 1px solid {COR_BORDA};
                    border-radius: 6px;
                    margin-top: 8px;
                    padding: 12px;
                    background-color: {COR_ELEVADA};
                }}
            """)
            lay_ol = QVBoxLayout(grp_ol)
            txt_ol = QLabel(
                "• Iniciar servico: systemctl start ollama\n"
                "• Baixar modelo recomendado: ollama pull qwen3:4b"
            )
            txt_ol.setStyleSheet("font-family: monospace; font-size: 11px; color: #96d6a8;")
            txt_ol.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            lay_ol.addWidget(txt_ol)
            layout.addWidget(grp_ol)

            # Botao Fechar
            btn_box = QHBoxLayout()
            btn_box.addStretch()
            btn_ok = QPushButton("Entendido")
            btn_ok.setStyleSheet(f"""
                QPushButton {{
                    background-color: {COR_PRIMARIA};
                    color: {COR_TEXTO_PRIMARIO};
                    font-weight: bold;
                    border: none;
                    border-radius: 6px;
                    padding: 6px 20px;
                }}
                QPushButton:hover {{
                    background-color: #b5ddfa;
                }}
            """)
            btn_ok.clicked.connect(self.accept)
            btn_box.addWidget(btn_ok)
            layout.addLayout(btn_box)


    class AbaFleetUsage(QWidget):
        """Aba AI Usage Bar com monitoramento em tempo real de N Cascos."""

        def __init__(self, parent: QWidget | None = None) -> None:
            super().__init__(parent)
            self.layout_principal = QVBoxLayout(self)
            self.layout_principal.setContentsMargins(12, 12, 12, 12)
            self.layout_principal.setSpacing(12)

            # Titulo e descricao
            topo = QHBoxLayout()
            titulo = QLabel("Frota de Cascos e Monitor de Cotas (AI Usage Bar)")
            titulo.setStyleSheet(f"font-size: 15px; font-weight: bold; color: {COR_TEXTO};")
            topo.addWidget(titulo)

            self.btn_nova_conta = QPushButton("+ Nova Conta / Casco")
            self.btn_nova_conta.setStyleSheet(f"""
                QPushButton {{
                    background-color: {COR_PRIMARIA};
                    color: {COR_TEXTO_PRIMARIO};
                    border: none;
                    border-radius: 6px;
                    padding: 5px 12px;
                    font-weight: bold;
                }}
                QPushButton:hover {{
                    background-color: #b5ddfa;
                }}
            """)
            self.btn_nova_conta.clicked.connect(self.abrir_dialogo_nova_conta)
            topo.addWidget(self.btn_nova_conta, alignment=Qt.AlignmentFlag.AlignRight)

            self.btn_atualizar = QPushButton("Atualizar Cotas")
            self.btn_atualizar.setStyleSheet(f"""
                QPushButton {{
                    background-color: {COR_ELEVADA};
                    color: {COR_PRIMARIA};
                    border: 1px solid {COR_BORDA};
                    border-radius: 6px;
                    padding: 5px 12px;
                    font-weight: bold;
                }}
                QPushButton:hover {{
                    background-color: {COR_ELEVADA_HOVER};
                }}
            """)
            topo.addWidget(self.btn_atualizar, alignment=Qt.AlignmentFlag.AlignRight)
            self.layout_principal.addLayout(topo)

            # Area rolavel de cartoes de cascos
            self.scroll = QScrollArea()
            self.scroll.setWidgetResizable(True)
            self.scroll.setStyleSheet("QScrollArea { border: none; background: transparent; }")
            self.container_cascos = QWidget()
            self.layout_cascos = QVBoxLayout(self.container_cascos)
            self.layout_cascos.setSpacing(10)
            self.scroll.setWidget(self.container_cascos)
            self.layout_principal.addWidget(self.scroll)

        def abrir_dialogo_nova_conta(self) -> None:
            dlg = DialogoNovaConta(self)
            dlg.exec()

        def atualizar_cascos(self, lista_cascos: list[dict[str, Any]]) -> None:
            # Limpa cartoes existentes
            while self.layout_cascos.count():
                item = self.layout_cascos.takeAt(0)
                w = item.widget()
                if w:
                    w.deleteLater()

            for c in lista_cascos:
                cartao = CartaoCasco(c, self.container_cascos)
                self.layout_cascos.addWidget(cartao)

            self.layout_cascos.addStretch()


    class AbaValidacao(QWidget):
        """Aba de Validacao Cruzada Independente e Portao Deterministico Local."""

        def __init__(self, parent: QWidget | None = None) -> None:
            super().__init__(parent)
            layout = QVBoxLayout(self)
            layout.setContentsMargins(14, 14, 14, 14)
            layout.setSpacing(12)

            # Topo com titulo e botoes de acao
            topo = QHBoxLayout()
            lbl_tit = QLabel("Validação Cruzada e Portão Local Offline")
            lbl_tit.setStyleSheet(f"font-size: 15px; font-weight: bold; color: {COR_TEXTO};")
            topo.addWidget(lbl_tit)
            topo.addStretch()

            self.btn_validar_local = QPushButton("▶ Executar Portão Deterministico (Zero Cota)")
            self.btn_validar_local.setStyleSheet(f"""
                QPushButton {{
                    background-color: {COR_PRIMARIA};
                    color: {COR_TEXTO_PRIMARIO};
                    border: none;
                    border-radius: 6px;
                    padding: 6px 14px;
                    font-weight: bold;
                    font-size: 12px;
                }}
                QPushButton:hover {{ background-color: #b5ddfa; }}
            """)
            self.btn_validar_local.clicked.connect(lambda: self.disparar_validacao(so_deterministico=True))
            topo.addWidget(self.btn_validar_local)

            self.btn_validar_revisor = QPushButton("🛡 Revisão Cruzada (Conta Oposta / 3P)")
            self.btn_validar_revisor.setStyleSheet(f"""
                QPushButton {{
                    background-color: #3b2d54;
                    color: {COR_REVISAO};
                    border: 1px solid rgba(201, 182, 242, 0.5);
                    border-radius: 6px;
                    padding: 6px 14px;
                    font-weight: bold;
                    font-size: 12px;
                }}
                QPushButton:hover {{ background-color: #4b3a6b; }}
            """)
            self.btn_validar_revisor.clicked.connect(lambda: self.disparar_validacao(so_deterministico=False))
            topo.addWidget(self.btn_validar_revisor)

            self.btn_abrir_fotos = QPushButton("📸 Ver Fotos")
            self.btn_abrir_fotos.setStyleSheet(f"""
                QPushButton {{
                    background-color: {COR_ELEVADA};
                    color: {COR_TEXTO_SUAVE};
                    border: 1px solid {COR_BORDA};
                    border-radius: 6px;
                    padding: 6px 12px;
                    font-size: 12px;
                }}
                QPushButton:hover {{ background-color: {COR_ELEVADA_HOVER}; }}
            """)
            self.btn_abrir_fotos.clicked.connect(self.abrir_pasta_fotos)
            topo.addWidget(self.btn_abrir_fotos)

            layout.addLayout(topo)

            # Pilares do Portao Deterministico
            grp_portao = QGroupBox("1. Pilares do Portão Deterministico Local (Zero Consumo de Nuvem)")
            grp_portao.setStyleSheet(f"""
                QGroupBox {{
                    color: {COR_TEXTO};
                    font-weight: bold;
                    border: 1px solid {COR_BORDA};
                    border-radius: 6px;
                    margin-top: 6px;
                    padding-top: 14px;
                    background-color: {COR_ELEVADA};
                }}
            """)
            lay_portao = QHBoxLayout(grp_portao)

            self.badge_gitleaks = QLabel("Segredos (gitleaks): OK")
            self.badge_bash = QLabel("Sintaxe Shell: OK")
            self.badge_python = QLabel("Compilação Python: OK")
            self.badge_testes = QLabel("Suíte Testes: OK")

            for b in (self.badge_gitleaks, self.badge_bash, self.badge_python, self.badge_testes):
                b.setStyleSheet(f"background-color: #1c2e24; color: {COR_CONCLUIDA}; border-radius: 4px; padding: 8px 12px; font-weight: bold; font-size: 11px;")
                lay_portao.addWidget(b)
            layout.addWidget(grp_portao)

            # Parecer da Revisao Cruzada
            grp_revisao = QGroupBox("2. Parecer do Revisor Independente na Conta Oposta (Claude Opus / Argon)")
            grp_revisao.setStyleSheet(f"""
                QGroupBox {{
                    color: {COR_TEXTO};
                    font-weight: bold;
                    border: 1px solid {COR_BORDA};
                    border-radius: 6px;
                    margin-top: 6px;
                    padding-top: 14px;
                    background-color: {COR_ELEVADA};
                }}
            """)
            lay_rev = QVBoxLayout(grp_revisao)
            lay_rev.setSpacing(8)

            linha_meta = QHBoxLayout()
            self.lbl_rev_modelo = QLabel("REVISOR: claude-opus-5-5-high (Familia 3P)")
            self.lbl_rev_modelo.setStyleSheet(f"color: {COR_PRIMARIA}; font-weight: bold; font-size: 12px;")
            linha_meta.addWidget(self.lbl_rev_modelo)

            self.lbl_rev_conta = QLabel("CONTA: conta2 (autor foi conta1)")
            self.lbl_rev_conta.setStyleSheet(f"color: {COR_TEXTO_SUAVE}; font-size: 11px;")
            linha_meta.addWidget(self.lbl_rev_conta, alignment=Qt.AlignmentFlag.AlignRight)

            self.badge_veredito = QLabel("VEREDITO: APROVADO")
            self.badge_veredito.setStyleSheet(f"background-color: #1c2e24; color: {COR_CONCLUIDA}; border-radius: 4px; padding: 4px 10px; font-weight: bold; font-size: 11px;")
            linha_meta.addWidget(self.badge_veredito, alignment=Qt.AlignmentFlag.AlignRight)
            lay_rev.addLayout(linha_meta)

            self.txt_parecer = QTextEdit()
            self.txt_parecer.setReadOnly(True)
            self.txt_parecer.setMinimumHeight(110)
            self.txt_parecer.setStyleSheet(f"""
                QTextEdit {{
                    background-color: #0b0e11;
                    color: {COR_TEXTO};
                    border: 1px solid {COR_BORDA};
                    border-radius: 6px;
                    padding: 8px;
                    font-family: Inter, sans-serif;
                    font-size: 12px;
                }}
            """)
            self.txt_parecer.setText("Nenhum parecer de revisor executado recentemente.")
            lay_rev.addWidget(self.txt_parecer)

            self.lbl_foto_info = QLabel("Foto: Nenhuma foto congelada inspecionada ainda.")
            self.lbl_foto_info.setStyleSheet(f"font-family: monospace; font-size: 11px; color: {COR_TEXTO_SUAVE};")
            lay_rev.addWidget(self.lbl_foto_info)

            layout.addWidget(grp_revisao)
            layout.addStretch()

        def abrir_pasta_fotos(self) -> None:
            pasta_fotos = obter_pasta_estado() / "fotos"
            pasta_fotos.mkdir(parents=True, exist_ok=True)
            subprocess.Popen(["xdg-open", str(pasta_fotos)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        def disparar_validacao(self, so_deterministico: bool) -> None:
            tipo_str = "portão deterministico local" if so_deterministico else "revisão cruzada na conta oposta"
            cmd = [str(RAIZ_CATAMARA / "bin" / "catamara-validar"), "--json"]
            if so_deterministico:
                cmd.append("--so-deterministico")

            # Identifica worktree ou repositorio da sessao ativa
            pasta_ses = obter_pasta_estado() / "sessoes"
            if pasta_ses.is_dir():
                for arq in sorted(pasta_ses.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
                    try:
                        d = json.loads(arq.read_text(encoding="utf-8"))
                        if d.get("estado") == "ativa":
                            cand = d.get("worktree") or d.get("repo")
                            if cand and Path(cand).exists():
                                cmd.extend(["--repo", str(cand)])
                                if d.get("conta"):
                                    cmd.extend(["--conta-autor", str(d["conta"])])
                                break
                    except Exception:
                        continue

            QMessageBox.information(self, "Iniciando Validação", f"Executando {tipo_str} em segundo plano.\nAo finalizar, o status será atualizado automaticamente.")
            
            def rodar():
                try:
                    subprocess.run(cmd, capture_output=True, text=True, check=False)
                except Exception:
                    pass

            import threading
            threading.Thread(target=rodar, daemon=True).start()

        def atualizar_validacao(self, historico_validar: list[dict[str, Any]]) -> None:
            if not historico_validar:
                return
            ult = historico_validar[0]
            det_ok = ult.get("deterministico_ok") is True and ult.get("codigo") == 0
            cor_det = COR_CONCLUIDA if det_ok else COR_FALHOU
            bg_det = "#1c2e24" if det_ok else "#2e1c1c"
            st_det = "PASSED" if det_ok else "FAILED"

            for b, nome in (
                (self.badge_gitleaks, "Segredos (gitleaks)"),
                (self.badge_bash, "Sintaxe Shell"),
                (self.badge_python, "Compilação Python"),
                (self.badge_testes, "Suíte Testes"),
            ):
                b.setText(f"{nome}: {st_det}")
                b.setStyleSheet(f"background-color: {bg_det}; color: {cor_det}; border-radius: 4px; padding: 8px 12px; font-weight: bold; font-size: 11px;")

            if ult.get("modelo"):
                self.lbl_rev_modelo.setText(f"REVISOR: {ult.get('modelo')}")
            if ult.get("conta_revisor"):
                self.lbl_rev_conta.setText(f"CONTA: {ult.get('conta_revisor')} (autor foi {ult.get('conta_autor') or 'oposta'})")
            if ult.get("foto"):
                self.lbl_foto_info.setText(f"Foto: {ult.get('foto')} (somente-leitura)")

            veredito = ult.get("veredito", "pendente").upper()
            aprovado = "APROVADO" in veredito
            cor_ver = COR_CONCLUIDA if aprovado else COR_FALHOU
            bg_ver = "#1c2e24" if aprovado else "#2e1c1c"
            self.badge_veredito.setText(f"VEREDITO: {veredito}")
            self.badge_veredito.setStyleSheet(f"background-color: {bg_ver}; color: {cor_ver}; border-radius: 4px; padding: 4px 10px; font-weight: bold; font-size: 11px;")

            if ult.get("texto_revisao"):
                self.txt_parecer.setText(ult["texto_revisao"])
            elif aprovado:
                self.txt_parecer.setText("Checagens deterministicas aprovadas sem violacao de contratos de integridade.")


    class AbaIndicadores(QWidget):
        """Aba Painel de Indicadores Analiticos e Linha do Tempo."""

        def __init__(self, parent: QWidget | None = None) -> None:
            super().__init__(parent)
            layout = QVBoxLayout(self)
            layout.setContentsMargins(14, 14, 14, 14)
            layout.setSpacing(12)

            topo = QHBoxLayout()
            lbl_tit = QLabel("Painel de Indicadores e Telemetria de Frota")
            lbl_tit.setStyleSheet(f"font-size: 15px; font-weight: bold; color: {COR_TEXTO};")
            topo.addWidget(lbl_tit)
            topo.addStretch()

            self.btn_atualizar = QPushButton("↻ Atualizar Indicadores")
            self.btn_atualizar.setStyleSheet(f"""
                QPushButton {{
                    background-color: {COR_ELEVADA};
                    color: {COR_PRIMARIA};
                    border: 1px solid {COR_BORDA};
                    border-radius: 6px;
                    padding: 5px 12px;
                    font-weight: bold;
                }}
                QPushButton:hover {{ background-color: {COR_ELEVADA_HOVER}; }}
            """)
            topo.addWidget(self.btn_atualizar)
            layout.addLayout(topo)

            # 4 cards em grade 2x2
            grid = QHBoxLayout()

            # Card 1: Economia
            grp_eco = QGroupBox("1. Economia de Cota e Motores")
            grp_eco.setStyleSheet(f"QGroupBox {{ color: {COR_TEXTO}; font-weight: bold; border: 1px solid {COR_BORDA}; border-radius: 6px; padding: 12px; background: {COR_ELEVADA}; }}")
            lay_eco = QVBoxLayout(grp_eco)
            self.lbl_eco_poupada = QLabel("Cota Externa Poupada: 0%")
            self.lbl_eco_poupada.setStyleSheet(f"color: {COR_CONCLUIDA}; font-size: 13px; font-weight: bold;")
            lay_eco.addWidget(self.lbl_eco_poupada)
            self.lbl_eco_ollama = QLabel("• Ollama (qwen3:4b): 0 chamadas")
            self.lbl_eco_flash = QLabel("• Gemini Flash: 0 chamadas")
            self.lbl_eco_impl = QLabel("• Implementador: 0 entregas")
            for lbl in (self.lbl_eco_ollama, self.lbl_eco_flash, self.lbl_eco_impl):
                lbl.setStyleSheet(f"color: {COR_TEXTO_SUAVE}; font-size: 11px;")
                lay_eco.addWidget(lbl)
            lay_eco.addStretch()
            grid.addWidget(grp_eco)

            # Card 2: Balanço de Cascos
            grp_bal = QGroupBox("2. Balanço entre Cascos")
            grp_bal.setStyleSheet(f"QGroupBox {{ color: {COR_TEXTO}; font-weight: bold; border: 1px solid {COR_BORDA}; border-radius: 6px; padding: 12px; background: {COR_ELEVADA}; }}")
            lay_bal = QVBoxLayout(grp_bal)
            self.lbl_bal_ratio = QLabel("C1: 50% │ C2: 50%")
            self.lbl_bal_ratio.setStyleSheet(f"color: {COR_PRIMARIA}; font-size: 13px; font-weight: bold;")
            lay_bal.addWidget(self.lbl_bal_ratio)
            self.lbl_bal_c1 = QLabel("• Casco 1 (Argon Coordenador): 0 chamadas")
            self.lbl_bal_c2 = QLabel("• Casco 2 (Flash/Opus Executor): 0 chamadas")
            for lbl in (self.lbl_bal_c1, self.lbl_bal_c2):
                lbl.setStyleSheet(f"color: {COR_TEXTO_SUAVE}; font-size: 11px;")
                lay_bal.addWidget(lbl)
            lay_bal.addStretch()
            grid.addWidget(grp_bal)

            # Card 3: Funil de Qualidade
            grp_fun = QGroupBox("3. Funil de Qualidade")
            grp_fun.setStyleSheet(f"QGroupBox {{ color: {COR_TEXTO}; font-weight: bold; border: 1px solid {COR_BORDA}; border-radius: 6px; padding: 12px; background: {COR_ELEVADA}; }}")
            lay_fun = QVBoxLayout(grp_fun)
            self.lbl_fun_taxa = QLabel("Portão Local: 100% aprovação")
            self.lbl_fun_taxa.setStyleSheet(f"color: {COR_CONCLUIDA}; font-size: 13px; font-weight: bold;")
            lay_fun.addWidget(self.lbl_fun_taxa)
            self.lbl_fun_rev = QLabel("• Revisão Cruzada: 100% aprovação")
            self.lbl_fun_ent = QLabel("• Entregas Aceitas: 0 pendentes")
            for lbl in (self.lbl_fun_rev, self.lbl_fun_ent):
                lbl.setStyleSheet(f"color: {COR_TEXTO_SUAVE}; font-size: 11px;")
                lay_fun.addWidget(lbl)
            lay_fun.addStretch()
            grid.addWidget(grp_fun)

            layout.addLayout(grid)

            # Card 4: Linha do Tempo Recente de Eventos
            lbl_lt = QLabel("Linha do Tempo Recente de Eventos (Journal):")
            lbl_lt.setStyleSheet(f"font-weight: bold; color: {COR_TEXTO}; margin-top: 6px;")
            layout.addWidget(lbl_lt)

            self.tabela_eventos = QTableWidget(0, 3)
            self.tabela_eventos.setHorizontalHeaderLabels(["Horário", "Evento", "Detalhes"])
            self.tabela_eventos.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
            self.tabela_eventos.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
            self.tabela_eventos.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
            self.tabela_eventos.setStyleSheet(f"""
                QTableWidget {{
                    background-color: {COR_ELEVADA};
                    color: {COR_TEXTO};
                    border: 1px solid {COR_BORDA};
                    border-radius: 6px;
                }}
                QHeaderView::section {{
                    background-color: #262b30;
                    color: {COR_PRIMARIA};
                    font-weight: bold;
                    padding: 4px;
                    border: none;
                }}
            """)
            layout.addWidget(self.tabela_eventos)

        def atualizar_indicadores(self, ind: dict[str, Any]) -> None:
            eco = ind.get("economia_motores", {})
            self.lbl_eco_poupada.setText(f"Cota Externa Poupada: {eco.get('cota_poupada_pct', 0)}%")
            por_mot = eco.get("por_motor", {})
            ol = por_mot.get("ollama", {})
            fl = por_mot.get("agy_flash", {})
            imp = por_mot.get("implementador", {})
            self.lbl_eco_ollama.setText(f"• Ollama (qwen3:4b): {ol.get('total', 0)} chamadas ({ol.get('taxa_sucesso_pct', 100)}% ok)")
            self.lbl_eco_flash.setText(f"• Gemini Flash: {fl.get('total', 0)} chamadas ({fl.get('taxa_sucesso_pct', 100)}% ok)")
            self.lbl_eco_impl.setText(f"• Implementador: {imp.get('total', 0)} entregas")

            bal = ind.get("balanco_cascos", {})
            c1 = bal.get("conta1_chamadas", 0)
            c2 = bal.get("conta2_chamadas", 0)
            tot = max(1, c1 + c2)
            self.lbl_bal_ratio.setText(f"C1: {int(c1/tot*100)}% │ C2: {int(c2/tot*100)}%")
            self.lbl_bal_c1.setText(f"• Casco 1 (Argon Coordenador): {c1} chamadas")
            self.lbl_bal_c2.setText(f"• Casco 2 (Flash/Opus Executor): {c2} chamadas")

            fun = ind.get("funil_qualidade", {})
            self.lbl_fun_taxa.setText(f"Portão Local: {fun.get('taxa_deterministico_pct', 100)}% aprovação")
            self.lbl_fun_rev.setText(f"• Revisão Cruzada: {fun.get('taxa_revisao_pct', 100)}% aprovação")
            self.lbl_fun_ent.setText(f"• Entregas Aceitas: {fun.get('implementacoes_aceitas', 0)} aceitas │ {fun.get('implementacoes_pendentes', 0)} pendentes")

            eventos = ind.get("linha_do_tempo", [])
            self.tabela_eventos.setRowCount(len(eventos))
            for i, ev in enumerate(eventos):
                ts = str(ev.get("ts", ""))[:19].replace("T", " ")
                self.tabela_eventos.setItem(i, 0, QTableWidgetItem(ts))
                self.tabela_eventos.setItem(i, 1, QTableWidgetItem(ev.get("titulo", "")))
                self.tabela_eventos.setItem(i, 2, QTableWidgetItem(ev.get("detalhes", "")))


    class AbaJail(QWidget):
        """Aba AI Jail com monitoramento de sandboxes Bubblewrap e portao deterministico."""

        def __init__(self, parent: QWidget | None = None) -> None:
            super().__init__(parent)
            layout = QVBoxLayout(self)
            layout.setContentsMargins(12, 12, 12, 12)
            layout.setSpacing(12)

            # Cabecalho
            lbl_titulo = QLabel("Confinamento Bubblewrap e Portao Local (AI Jail)")
            lbl_titulo.setStyleSheet(f"font-size: 15px; font-weight: bold; color: {COR_TEXTO};")
            layout.addWidget(lbl_titulo)

            # Pilares do Portao Deterministico
            grupo_portao = QGroupBox("Pilares do Portao Deterministico Offline")
            grupo_portao.setStyleSheet(f"QGroupBox {{ color: {COR_TEXTO}; font-weight: bold; border: 1px solid {COR_BORDA}; border-radius: 6px; margin-top: 10px; padding-top: 12px; }}")
            lay_portao = QHBoxLayout(grupo_portao)

            self.badge_conflitos = QLabel("Conflitos Git: PASSED")
            self.badge_sintaxe = QLabel("Sintaxe Shell/Py: PASSED")
            self.badge_gitleaks = QLabel("Gitleaks (Segredos): PASSED")
            self.badge_testes = QLabel("Suíte Testes: PASSED")

            for badge in (self.badge_conflitos, self.badge_sintaxe, self.badge_gitleaks, self.badge_testes):
                badge.setStyleSheet(f"background-color: #1c2e24; color: {COR_CONCLUIDA}; border-radius: 4px; padding: 6px 10px; font-weight: bold; font-size: 11px;")
                lay_portao.addWidget(badge)
            layout.addWidget(grupo_portao)

            # Tabela de Processos Confinados
            lbl_proc = QLabel("Processos Confinados Ativos (Bubblewrap):")
            lbl_proc.setStyleSheet(f"font-weight: bold; color: {COR_TEXTO}; margin-top: 8px;")
            layout.addWidget(lbl_proc)

            self.tabela_proc = QTableWidget(0, 5)
            self.tabela_proc.setHorizontalHeaderLabels(["PID", "Conta", "Perfil", "Comando", "Iniciado em"])
            self.tabela_proc.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
            self.tabela_proc.setStyleSheet(f"""
                QTableWidget {{
                    background-color: {COR_ELEVADA};
                    color: {COR_TEXTO};
                    border: 1px solid {COR_BORDA};
                    border-radius: 6px;
                }}
                QHeaderView::section {{
                    background-color: #262b30;
                    color: {COR_PRIMARIA};
                    font-weight: bold;
                    padding: 4px;
                    border: none;
                }}
            """)
            layout.addWidget(self.tabela_proc)

        def atualizar_processos(self, processos: list[dict[str, Any]]) -> None:
            self.tabela_proc.setRowCount(len(processos))
            for i, p in enumerate(processos):
                self.tabela_proc.setItem(i, 0, QTableWidgetItem(str(p.get("pid", ""))))
                self.tabela_proc.setItem(i, 1, QTableWidgetItem(str(p.get("conta", ""))))
                self.tabela_proc.setItem(i, 2, QTableWidgetItem(str(p.get("perfil", ""))))
                self.tabela_proc.setItem(i, 3, QTableWidgetItem(str(p.get("comando", ""))))
                self.tabela_proc.setItem(i, 4, QTableWidgetItem(str(p.get("iniciado_em", ""))))


    class AbaEntregas(QWidget):
        """Aba AI Memory & Entregas Delimitadas com visualizador de diff em JetBrains Mono."""

        def __init__(self, parent: QWidget | None = None) -> None:
            super().__init__(parent)
            self.implementacoes: list[dict[str, Any]] = []
            layout = QVBoxLayout(self)
            layout.setContentsMargins(12, 12, 12, 12)
            layout.setSpacing(10)

            splitter = QSplitter(Qt.Orientation.Horizontal)

            # Painel esquerdo: lista de entregas pendentes
            painel_esq = QWidget()
            lay_esq = QVBoxLayout(painel_esq)
            lay_esq.setContentsMargins(0, 0, 0, 0)

            lbl_lista = QLabel("Entregas Delimitadas:")
            lbl_lista.setStyleSheet(f"font-weight: bold; color: {COR_TEXTO};")
            lay_esq.addWidget(lbl_lista)

            self.tabela_entregas = QTableWidget(0, 3)
            self.tabela_entregas.setHorizontalHeaderLabels(["ID", "Estado", "Tarefa"])
            self.tabela_entregas.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
            self.tabela_entregas.setStyleSheet(f"""
                QTableWidget {{
                    background-color: {COR_ELEVADA};
                    color: {COR_TEXTO};
                    border: 1px solid {COR_BORDA};
                    border-radius: 6px;
                }}
                QHeaderView::section {{
                    background-color: #262b30;
                    color: {COR_PRIMARIA};
                    font-weight: bold;
                    padding: 4px;
                    border: none;
                }}
            """)
            self.tabela_entregas.itemSelectionChanged.connect(self.ao_selecionar_entrega)
            lay_esq.addWidget(self.tabela_entregas)
            splitter.addWidget(painel_esq)

            # Painel direito: visualizador de diff e botao de aceite
            painel_dir = QWidget()
            lay_dir = QVBoxLayout(painel_dir)
            lay_dir.setContentsMargins(0, 0, 0, 0)

            topo_diff = QHBoxLayout()
            self.lbl_detalhe_id = QLabel("Nenhuma entrega selecionada")
            self.lbl_detalhe_id.setStyleSheet(f"font-weight: bold; color: {COR_PRIMARIA}; font-size: 13px;")
            topo_diff.addWidget(self.lbl_detalhe_id)
            topo_diff.addStretch()

            self.btn_rejeitar = QPushButton("✕ Rejeitar")
            self.btn_rejeitar.setEnabled(False)
            self.btn_rejeitar.setStyleSheet(f"""
                QPushButton {{
                    background-color: #2e1c1c;
                    color: {COR_FALHOU};
                    border: 1px solid rgba(240, 157, 147, 0.4);
                    border-radius: 6px;
                    padding: 6px 12px;
                    font-size: 12px;
                }}
                QPushButton:hover {{
                    background-color: #3e2424;
                }}
                QPushButton:disabled {{
                    background-color: #1c2024;
                    color: #555;
                    border: 1px solid #333;
                }}
            """)
            self.btn_rejeitar.clicked.connect(self.ao_clicar_rejeitar)
            topo_diff.addWidget(self.btn_rejeitar)

            self.btn_aceitar = QPushButton("✓ Aceitar Entrega (Alt+A)")
            self.btn_aceitar.setEnabled(False)
            self.btn_aceitar.setStyleSheet(f"""
                QPushButton {{
                    background-color: #1e3d2f;
                    color: {COR_CONCLUIDA};
                    border: 1px solid {COR_CONCLUIDA};
                    border-radius: 6px;
                    padding: 6px 14px;
                    font-weight: bold;
                    font-size: 12px;
                }}
                QPushButton:hover {{
                    background-color: #27523f;
                }}
                QPushButton:disabled {{
                    background-color: #1c2024;
                    color: #555;
                    border: 1px solid #333;
                }}
            """)
            self.btn_aceitar.clicked.connect(self.ao_clicar_aceitar)
            topo_diff.addWidget(self.btn_aceitar)
            lay_dir.addLayout(topo_diff)

            # Visualizador de Diff em JetBrains Mono
            self.txt_diff = QTextEdit()
            self.txt_diff.setReadOnly(True)
            fonte_mono = QFont("JetBrains Mono", 10)
            fonte_mono.setStyleHint(QFont.StyleHint.Monospace)
            self.txt_diff.setFont(fonte_mono)
            self.txt_diff.setStyleSheet(f"""
                QTextEdit {{
                    background-color: #0b0e11;
                    color: {COR_TEXTO};
                    border: 1px solid {COR_BORDA};
                    border-radius: 6px;
                    padding: 8px;
                }}
            """)
            lay_dir.addWidget(self.txt_diff)
            splitter.addWidget(painel_dir)

            splitter.setStretchFactor(0, 1)
            splitter.setStretchFactor(1, 2)
            layout.addWidget(splitter)

        def atualizar_entregas(self, lista: list[dict[str, Any]]) -> None:
            self.implementacoes = lista
            self.tabela_entregas.setRowCount(len(lista))
            for i, item in enumerate(lista):
                self.tabela_entregas.setItem(i, 0, QTableWidgetItem(item.get("id", "")))
                st_item = QTableWidgetItem(item.get("estado", ""))
                if item.get("pendente"):
                    st_item.setForeground(QColor(COR_BLOQUEADA))
                elif item.get("estado") == "aceito":
                    st_item.setForeground(QColor(COR_CONCLUIDA))
                self.tabela_entregas.setItem(i, 1, st_item)
                self.tabela_entregas.setItem(i, 2, QTableWidgetItem(item.get("tarefa", "")))

        def ao_selecionar_entrega(self) -> None:
            row = self.tabela_entregas.currentRow()
            if 0 <= row < len(self.implementacoes):
                item = self.implementacoes[row]
                self.lbl_detalhe_id.setText(f"Entrega {item.get('id', '')} (Base: {item.get('base', '')[:10]})")
                entrega = item.get("entrega") or {}
                diff_texto = entrega.get("diff", "(sem diff gerado)")
                self.exibir_diff_colorido(diff_texto)
                self.btn_aceitar.setEnabled(bool(item.get("pendente")))
                self.btn_rejeitar.setEnabled(bool(item.get("pendente")))
            else:
                self.lbl_detalhe_id.setText("Nenhuma entrega selecionada")
                self.txt_diff.clear()
                self.btn_aceitar.setEnabled(False)
                self.btn_rejeitar.setEnabled(False)

        def exibir_diff_colorido(self, texto: str) -> None:
            self.txt_diff.clear()
            cursor = self.txt_diff.textCursor()
            linhas = texto.splitlines()
            for l in linhas:
                if l.startswith("+") and not l.startswith("+++"):
                    cor = COR_CONCLUIDA
                elif l.startswith("-") and not l.startswith("---"):
                    cor = COR_FALHOU
                elif l.startswith("@@"):
                    cor = COR_REVISAO
                else:
                    cor = COR_TEXTO_SUAVE
                self.txt_diff.append(f"<span style='color:{cor}; font-family:JetBrains Mono, monospace;'>{l.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')}</span>")

        def ao_clicar_rejeitar(self) -> None:
            row = self.tabela_entregas.currentRow()
            if 0 <= row < len(self.implementacoes):
                item = self.implementacoes[row]
                impl_id = item.get("id", "")
                res = QMessageBox.question(
                    self,
                    "Rejeitar Entrega",
                    f"Deseja rejeitar a entrega {impl_id} e solicitar ajuste?",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                )
                if res == QMessageBox.StandardButton.Yes:
                    try:
                        implementacao.rejeitar(impl_id, motivo="Rejeitado na central gráfica")
                        QMessageBox.information(self, "Rejeitada", f"Entrega {impl_id} rejeitada com sucesso.")
                        janela = self.window()
                        if hasattr(janela, "atualizar_dados"):
                            janela.atualizar_dados()
                    except Exception as exc:
                        QMessageBox.critical(self, "Erro", str(exc))

        def ao_clicar_aceitar(self) -> None:
            row = self.tabela_entregas.currentRow()
            if 0 <= row < len(self.implementacoes):
                item = self.implementacoes[row]
                impl_id = item.get("id", "")
                entrega = item.get("entrega") or {}
                entrega_hash = entrega.get("entrega_hash", "")
                if not entrega_hash:
                    QMessageBox.warning(self, "Aviso", "A entrega nao possui hash SHA-256 valido.")
                    return

                res = QMessageBox.question(
                    self,
                    "Confirmar Aceite",
                    f"Deseja aceitar atômicamente a entrega {impl_id} com conferência SHA-256 ({entrega_hash[:12]}...)?",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                )
                if res == QMessageBox.StandardButton.Yes:
                    try:
                        resultado = implementacao.aceitar(impl_id, entrega_hash)
                        if resultado.get("aceito"):
                            QMessageBox.information(
                                self,
                                "Sucesso",
                                f"Entrega {impl_id} aceita com sucesso!\nHash conferido: {entrega_hash[:16]}...",
                            )
                            janela = self.window()
                            if hasattr(janela, "atualizar_dados"):
                                janela.atualizar_dados()
                        else:
                            QMessageBox.critical(self, "Falha", f"Nao foi possivel aceitar entrega {impl_id}")
                    except Exception as exc:
                        QMessageBox.critical(self, "Erro", str(exc))


    def abrir_terminal_sessao(sid: str) -> None:
        """Abre ou foca o terminal da sessao tmux com suporte a Niri, Hyprland e emuladores variados."""
        # 1. Tenta focar no Niri
        if os.environ.get("NIRI_SOCKET"):
            try:
                proc = subprocess.run(
                    ["niri", "msg", "action", "focus-window", "--matching-title", f"Catamara: {sid}"],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                if proc.returncode == 0:
                    return
            except Exception:
                pass

        # 2. Tenta focar no Hyprland
        if os.environ.get("HYPRLAND_INSTANCE_SIGNATURE"):
            proc = subprocess.run(
                ["hyprctl", "dispatch", "focuswindow", f"title:^Catamara: {sid}$"],
                capture_output=True,
                text=True,
                check=False,
            )
            if proc.returncode == 0 and "ok" in proc.stdout.lower() and "warning" not in proc.stdout.lower():
                return

        # 3. Fallback: abre terminal com cata continuar
        cmd_cata = str(RAIZ_CATAMARA / "bin" / "cata")
        for term in ["jangada-terminal", "alacritty", "ghostty", "kitty", "foot", "wezterm", "konsole", "x-terminal-emulator", "xterm"]:
            term_bin = shutil.which(term)
            if term_bin:
                if term == "jangada-terminal":
                    cmd = [term_bin, "--classe", f"catamara-sessao-{sid}", "--titulo", f"Catamara: {sid}", "-e", cmd_cata, "continuar", sid]
                elif term in {"kitty", "ghostty", "foot"}:
                    cmd = [term_bin, "--title", f"Catamara: {sid}", cmd_cata, "continuar", sid]
                elif term == "alacritty":
                    cmd = [term_bin, "--class", f"catamara-sessao-{sid}", "--title", f"Catamara: {sid}", "-e", cmd_cata, "continuar", sid]
                elif term == "wezterm":
                    cmd = [term_bin, "start", "--class", f"catamara-sessao-{sid}", "--", cmd_cata, "continuar", sid]
                else:
                    cmd = [term_bin, "-e", cmd_cata, "continuar", sid]
                try:
                    subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
                    return
                except Exception:
                    continue

    def abrir_terminal_worktree(sid: str) -> None:
        """Abre um terminal direto na pasta da worktree da sessao."""
        s_arq = obter_pasta_estado() / "sessoes" / f"{sid}.json"
        wt_dir = None
        if s_arq.is_file():
            try:
                d = json.loads(s_arq.read_text(encoding="utf-8"))
                wt_dir = d.get("worktree") or d.get("repo")
            except Exception:
                pass
        if not wt_dir or not Path(wt_dir).exists():
            return

        for term in ["jangada-terminal", "alacritty", "ghostty", "kitty", "foot", "wezterm", "konsole", "x-terminal-emulator", "xterm"]:
            term_bin = shutil.which(term)
            if term_bin:
                if term == "jangada-terminal":
                    cmd = [term_bin, "--classe", f"catamara-worktree-{sid}", "--titulo", f"Worktree: {sid}", "-e", "bash"]
                elif term in {"kitty", "ghostty", "foot"}:
                    cmd = [term_bin, "--title", f"Worktree: {sid}", "bash"]
                elif term == "alacritty":
                    cmd = [term_bin, "--class", f"catamara-worktree-{sid}", "--title", f"Worktree: {sid}", "-e", "bash"]
                elif term == "wezterm":
                    cmd = [term_bin, "start", "--class", f"catamara-worktree-{sid}", "--", "bash"]
                else:
                    cmd = [term_bin, "-e", "bash"]
                try:
                    subprocess.Popen(cmd, cwd=str(wt_dir), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
                    return
                except Exception:
                    continue


    class DialogoNovaTarefa(QDialog):
        """Dialogo ergonomico para iniciar nova tarefa sem burocracia de pastas."""

        def __init__(self, parent: QWidget | None = None) -> None:
            super().__init__(parent)
            self.setWindowTitle("Catamarã · Iniciar Nova Tarefa")
            self.setMinimumSize(480, 260)
            self.resize(520, 290)
            self.sessao_criada: dict[str, Any] | None = None
            self.montar_ui()

        def montar_ui(self) -> None:
            layout = QVBoxLayout(self)
            layout.setContentsMargins(20, 20, 20, 20)
            layout.setSpacing(12)

            lbl_tit = QLabel("Iniciar Nova Tarefa")
            lbl_tit.setStyleSheet(f"font-size: 16px; font-weight: bold; color: {COR_PRIMARIA};")
            layout.addWidget(lbl_tit)

            # Campo 1: Projeto
            lbl_proj = QLabel("1. Projeto (onde você vai trabalhar):")
            lbl_proj.setStyleSheet(f"color: {COR_TEXTO}; font-weight: bold; font-size: 12px;")
            layout.addWidget(lbl_proj)

            self.combo_projeto = QComboBox()
            self.combo_projeto.setStyleSheet(f"""
                QComboBox {{
                    background-color: {COR_ELEVADA};
                    color: {COR_TEXTO};
                    border: 1px solid {COR_BORDA};
                    border-radius: 6px;
                    padding: 6px 10px;
                    font-size: 13px;
                }}
                QComboBox::drop-down {{ border: none; }}
                QComboBox QAbstractItemView {{
                    background-color: {COR_ELEVADA};
                    color: {COR_TEXTO};
                    selection-background-color: #1f4968;
                    border: 1px solid {COR_BORDA};
                }}
            """)

            projetos_base = Path(os.environ.get("CATAMARA_PROJETOS", Path.home() / "Projetos"))
            lista_projetos = []
            if RAIZ_CATAMARA.is_dir():
                lista_projetos.append({"nome": f"Catamarã ({RAIZ_CATAMARA.name})", "caminho": str(RAIZ_CATAMARA)})
            if projetos_base.is_dir():
                for d in sorted(projetos_base.iterdir()):
                    if d.is_dir() and not d.name.startswith(".") and d != RAIZ_CATAMARA:
                        lista_projetos.append({"nome": d.name, "caminho": str(d)})

            for p in lista_projetos:
                self.combo_projeto.addItem(p["nome"], p["caminho"])

            self.combo_projeto.addItem("+ Criar Novo Projeto em ~/Projetos...", "__NOVO__")
            self.combo_projeto.currentIndexChanged.connect(self.ao_mudar_projeto)
            layout.addWidget(self.combo_projeto)

            self.txt_novo_nome = QLineEdit()
            self.txt_novo_nome.setPlaceholderText("Nome da nova pasta em ~/Projetos (ex: meu-app)")
            self.txt_novo_nome.setStyleSheet(f"""
                QLineEdit {{
                    background-color: #0b0e11;
                    color: {COR_TEXTO};
                    border: 1px solid {COR_BORDA};
                    border-radius: 6px;
                    padding: 6px 10px;
                }}
            """)
            self.txt_novo_nome.hide()
            layout.addWidget(self.txt_novo_nome)

            # Campo 2: Objetivo da Tarefa
            lbl_tarefa = QLabel("2. Objetivo da Tarefa (o que vamos fazer):")
            lbl_tarefa.setStyleSheet(f"color: {COR_TEXTO}; font-weight: bold; font-size: 12px;")
            layout.addWidget(lbl_tarefa)

            self.txt_tarefa = QLineEdit()
            self.txt_tarefa.setPlaceholderText("Ex: refatorar-api, adicionar-login, ajustar-layout...")
            self.txt_tarefa.setStyleSheet(f"""
                QLineEdit {{
                    background-color: {COR_ELEVADA};
                    color: {COR_TEXTO};
                    border: 1px solid {COR_BORDA};
                    border-radius: 6px;
                    padding: 6px 10px;
                    font-size: 13px;
                }}
            """)
            layout.addWidget(self.txt_tarefa)

            # Campo 3: Casco / Agente Inicial
            lbl_motor = QLabel("3. Casco / Agente Inicial:")
            lbl_motor.setStyleSheet(f"color: {COR_TEXTO}; font-weight: bold; font-size: 12px;")
            layout.addWidget(lbl_motor)

            self.combo_motor = QComboBox()
            self.combo_motor.setStyleSheet(f"""
                QComboBox {{
                    background-color: {COR_ELEVADA};
                    color: {COR_TEXTO};
                    border: 1px solid {COR_BORDA};
                    border-radius: 6px;
                    padding: 6px 10px;
                    font-size: 13px;
                }}
                QComboBox::drop-down {{ border: none; }}
                QComboBox QAbstractItemView {{
                    background-color: {COR_ELEVADA};
                    color: {COR_TEXTO};
                    selection-background-color: #1f4968;
                    border: 1px solid {COR_BORDA};
                }}
            """)
            self.combo_motor.addItem("Antigravity CLI (Argon / Flash Coordenador)", "agy")
            self.combo_motor.addItem("Claude Code CLI (claude)", "claude")
            self.combo_motor.addItem("Codex CLI (codex - OpenAI)", "codex")
            self.combo_motor.addItem("Ollama Local (qwen2.5-coder offline)", "ollama")
            layout.addWidget(self.combo_motor)

            lbl_dica = QLabel("🛡️ O isolamento em worktree Git é automático. Seu repositório principal fica protegido.")
            lbl_dica.setStyleSheet(f"color: {COR_TEXTO_SUAVE}; font-size: 11px;")
            layout.addWidget(lbl_dica)

            layout.addStretch()

            # Botoes
            botoes = QHBoxLayout()
            botoes.addStretch()
            btn_cancelar = QPushButton("Cancelar")
            btn_cancelar.setStyleSheet(f"""
                QPushButton {{
                    background-color: {COR_ELEVADA};
                    color: {COR_TEXTO_SUAVE};
                    border: 1px solid {COR_BORDA};
                    border-radius: 6px;
                    padding: 6px 16px;
                }}
            """)
            btn_cancelar.clicked.connect(self.reject)
            botoes.addWidget(btn_cancelar)

            btn_iniciar = QPushButton("▶ Iniciar Tarefa no Terminal")
            btn_iniciar.setStyleSheet(f"""
                QPushButton {{
                    background-color: {COR_PRIMARIA};
                    color: {COR_TEXTO_PRIMARIO};
                    font-weight: bold;
                    border: none;
                    border-radius: 6px;
                    padding: 6px 18px;
                }}
                QPushButton:hover {{
                    background-color: #b5ddfa;
                }}
            """)
            btn_iniciar.clicked.connect(self.ao_iniciar)
            botoes.addWidget(btn_iniciar)
            layout.addLayout(botoes)

        def ao_mudar_projeto(self) -> None:
            caminho = self.combo_projeto.currentData()
            if caminho == "__NOVO__":
                self.txt_novo_nome.show()
                self.txt_novo_nome.setFocus()
            else:
                self.txt_novo_nome.hide()

        def ao_iniciar(self) -> None:
            caminho = self.combo_projeto.currentData()
            if caminho == "__NOVO__":
                nome = self.txt_novo_nome.text().strip()
                if not nome:
                    QMessageBox.warning(self, "Aviso", "Informe o nome para o novo projeto.")
                    return
                base_p = Path(os.environ.get("CATAMARA_PROJETOS", Path.home() / "Projetos"))
                alvo_p = base_p / nome
                try:
                    sessoes.inicializar_novo_repo_git(alvo_p, nome)
                    caminho = str(alvo_p)
                except Exception as exc:
                    QMessageBox.critical(self, "Erro", f"Falha ao criar projeto: {exc}")
                    return

            tarefa_slug = self.txt_tarefa.text().strip()
            sid = re.sub(r"[^a-z0-9-]", "-", tarefa_slug.lower()).strip("-") if tarefa_slug else None
            motor_escolhido = self.combo_motor.currentData() or "agy"

            try:
                dados = sessoes.criar_e_iniciar_sessao(
                    repo=caminho,
                    sessao_id=sid,
                    conta=os.environ.get("CATAMARA_CONTA_PADRAO", "conta1"),
                    direto=False,
                    motor=motor_escolhido,
                )
                self.sessao_criada = dados
                abrir_terminal_sessao(dados["sessao"])
                self.accept()
            except Exception as exc:
                QMessageBox.critical(self, "Erro ao iniciar tarefa", str(exc))


    class AbaTarefas(QWidget):
        """Aba principal de Projetos e Tarefas ativas (coracao da Central)."""

        def __init__(self, parent: QWidget | None = None) -> None:
            super().__init__(parent)
            self.sessoes_atuais: list[dict[str, Any]] = []
            layout = QVBoxLayout(self)
            layout.setContentsMargins(12, 12, 12, 12)
            layout.setSpacing(12)

            topo = QHBoxLayout()
            lbl_titulo = QLabel("Tarefas e Sessões de Desenvolvimento")
            lbl_titulo.setStyleSheet(f"font-size: 15px; font-weight: bold; color: {COR_TEXTO};")
            topo.addWidget(lbl_titulo)
            topo.addStretch()

            self.btn_atualizar = QPushButton("↻ Atualizar")
            self.btn_atualizar.setStyleSheet(f"""
                QPushButton {{
                    background-color: {COR_ELEVADA};
                    color: {COR_PRIMARIA};
                    border: 1px solid {COR_BORDA};
                    border-radius: 6px;
                    padding: 5px 14px;
                    font-weight: bold;
                }}
                QPushButton:hover {{
                    background-color: {COR_ELEVADA_HOVER};
                }}
            """)
            topo.addWidget(self.btn_atualizar)
            layout.addLayout(topo)

            # Card de estado vazio
            self.card_vazio = QFrame()
            self.card_vazio.setStyleSheet(f"""
                QFrame {{
                    background-color: {COR_ELEVADA};
                    border: 1px dashed {COR_BORDA};
                    border-radius: 8px;
                    padding: 36px;
                }}
            """)
            lay_vazio = QVBoxLayout(self.card_vazio)
            lay_vazio.setAlignment(Qt.AlignmentFlag.AlignCenter)
            lbl_vazio_ico = QLabel("⛵")
            lbl_vazio_ico.setStyleSheet(f"font-size: 34px; color: {COR_PRIMARIA};")
            lbl_vazio_ico.setAlignment(Qt.AlignmentFlag.AlignCenter)
            lay_vazio.addWidget(lbl_vazio_ico)
            lbl_vazio_tit = QLabel("Nenhuma tarefa em andamento no momento")
            lbl_vazio_tit.setStyleSheet(f"font-size: 15px; font-weight: bold; color: {COR_TEXTO};")
            lbl_vazio_tit.setAlignment(Qt.AlignmentFlag.AlignCenter)
            lay_vazio.addWidget(lbl_vazio_tit)
            lbl_vazio_sub = QLabel("Clique no botão '+ Nova Tarefa' no topo para iniciar um trabalho com o Coordenador Argon em ambiente isolado.")
            lbl_vazio_sub.setStyleSheet(f"color: {COR_TEXTO_SUAVE}; font-size: 12px; margin-top: 4px; margin-bottom: 8px;")
            lbl_vazio_sub.setAlignment(Qt.AlignmentFlag.AlignCenter)
            lay_vazio.addWidget(lbl_vazio_sub)

            self.btn_criar_vazio = QPushButton("+ Criar Primeira Tarefa")
            self.btn_criar_vazio.setStyleSheet(f"""
                QPushButton {{
                    background-color: {COR_PRIMARIA};
                    color: {COR_TEXTO_PRIMARIO};
                    font-weight: bold;
                    border: none;
                    border-radius: 6px;
                    padding: 8px 20px;
                    font-size: 12px;
                }}
                QPushButton:hover {{
                    background-color: #b5ddfa;
                }}
            """)
            self.btn_criar_vazio.clicked.connect(self.abrir_dialogo_nova_tarefa)
            lay_vazio.addWidget(self.btn_criar_vazio, alignment=Qt.AlignmentFlag.AlignCenter)

            layout.addWidget(self.card_vazio)

            # Tabela de tarefas com coluna de Data de Abertura
            self.tabela_tarefas = QTableWidget(0, 6)
            self.tabela_tarefas.setHorizontalHeaderLabels([
                "Projeto",
                "Tarefa / Sessão",
                "Data de Abertura",
                "Casco Inicial",
                "Status",
                "Ações",
            ])
            self.tabela_tarefas.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
            self.tabela_tarefas.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
            self.tabela_tarefas.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
            self.tabela_tarefas.horizontalHeader().setSectionResizeMode(5, QHeaderView.ResizeMode.ResizeToContents)
            self.tabela_tarefas.setStyleSheet(f"""
                QTableWidget {{
                    background-color: {COR_ELEVADA};
                    color: {COR_TEXTO};
                    border: 1px solid {COR_BORDA};
                    border-radius: 6px;
                }}
                QHeaderView::section {{
                    background-color: #262b30;
                    color: {COR_PRIMARIA};
                    font-weight: bold;
                    padding: 6px;
                    border: none;
                }}
            """)
            layout.addWidget(self.tabela_tarefas)

        def abrir_dialogo_nova_tarefa(self) -> None:
            dlg = DialogoNovaTarefa(self)
            if dlg.exec() == QDialog.DialogCode.Accepted:
                janela = self.window()
                if hasattr(janela, "atualizar_dados"):
                    janela.atualizar_dados()

        def atualizar_sessoes(self, sessoes_lista: list[dict[str, Any]]) -> None:
            self.sessoes_atuais = sessoes_lista
            if not sessoes_lista:
                self.card_vazio.show()
                self.tabela_tarefas.hide()
                return

            self.card_vazio.hide()
            self.tabela_tarefas.show()
            self.tabela_tarefas.setRowCount(len(sessoes_lista))

            for i, s in enumerate(sessoes_lista):
                sid = s.get("sessao") or ""
                proj = s.get("projeto") or Path(s.get("repo", "")).name or "Projeto"
                conta = s.get("conta") or "conta1"
                ativa = s.get("ativa_no_tmux", False) or s.get("estado") == "ativa"

                # Formata data de abertura amigavel (ex: 22/10 00:30)
                criado_raw = str(s.get("criado") or "")
                data_formatada = "--"
                if criado_raw:
                    try:
                        dt = datetime.fromisoformat(criado_raw)
                        data_formatada = dt.strftime("%d/%m %H:%M")
                    except Exception:
                        partes = criado_raw.split("T")
                        if len(partes) == 2:
                            d_part = partes[0].split("-")
                            h_part = partes[1][:5]
                            if len(d_part) == 3:
                                data_formatada = f"{d_part[2]}/{d_part[1]} {h_part}"
                        else:
                            data_formatada = criado_raw[:16]

                self.tabela_tarefas.setItem(i, 0, QTableWidgetItem(proj))
                self.tabela_tarefas.setItem(i, 1, QTableWidgetItem(sid))

                item_data = QTableWidgetItem(data_formatada)
                item_data.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                item_data.setForeground(QColor(COR_TEXTO_SUAVE))
                self.tabela_tarefas.setItem(i, 2, item_data)

                motor = s.get("motor") or "agy"
                if motor == "claude":
                    casco_desc = "Claude Code"
                elif motor == "codex":
                    casco_desc = "Codex CLI"
                elif motor == "ollama":
                    casco_desc = "Ollama Local"
                else:
                    casco_desc = f"{conta} (Argon)" if conta == "conta1" else f"{conta} (Flash)"

                self.tabela_tarefas.setItem(i, 3, QTableWidgetItem(casco_desc))
                st_item = QTableWidgetItem("● Ativa no tmux" if ativa else "○ Pausada / Finalizada")
                st_item.setForeground(QColor(COR_CONCLUIDA if ativa else COR_TEXTO_SUAVE))
                self.tabela_tarefas.setItem(i, 4, st_item)

                widget_acoes = QWidget()
                lay_ac = QHBoxLayout(widget_acoes)
                lay_ac.setContentsMargins(4, 2, 4, 2)
                lay_ac.setSpacing(6)

                btn_continuar = QPushButton("▶ Focar / Continuar")
                btn_continuar.setStyleSheet(f"""
                    QPushButton {{
                        background-color: #1e3d2f;
                        color: {COR_CONCLUIDA};
                        border: 1px solid rgba(150, 214, 168, 0.4);
                        border-radius: 4px;
                        padding: 4px 8px;
                        font-size: 11px;
                        font-weight: bold;
                    }}
                    QPushButton:hover {{
                        background-color: #27523f;
                    }}
                """)
                btn_continuar.clicked.connect(lambda _, x=sid: abrir_terminal_sessao(x))
                lay_ac.addWidget(btn_continuar)

                btn_worktree = QPushButton("📂 Worktree")
                btn_worktree.setStyleSheet(f"""
                    QPushButton {{
                        background-color: {COR_ELEVADA};
                        color: {COR_PRIMARIA};
                        border: 1px solid {COR_BORDA};
                        border-radius: 4px;
                        padding: 4px 8px;
                        font-size: 11px;
                        font-weight: bold;
                    }}
                    QPushButton:hover {{
                        background-color: {COR_ELEVADA_HOVER};
                    }}
                """)
                btn_worktree.clicked.connect(lambda _, x=sid: abrir_terminal_worktree(x))
                lay_ac.addWidget(btn_worktree)

                btn_encerrar = QPushButton("✕ Encerrar")
                btn_encerrar.setStyleSheet(f"""
                    QPushButton {{
                        background-color: #2e1c1c;
                        color: {COR_FALHOU};
                        border: 1px solid rgba(240, 157, 147, 0.3);
                        border-radius: 4px;
                        padding: 4px 8px;
                        font-size: 11px;
                    }}
                    QPushButton:hover {{
                        background-color: #3e2424;
                    }}
                """)
                btn_encerrar.clicked.connect(lambda _, x=sid: self.encerrar_sessao(x))
                lay_ac.addWidget(btn_encerrar)

                self.tabela_tarefas.setCellWidget(i, 5, widget_acoes)

        def encerrar_sessao(self, sid: str) -> None:
            res = QMessageBox.question(
                self,
                "Encerrar Sessão",
                f"Deseja encerrar o container e finalizar a sessão '{sid}'?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if res == QMessageBox.StandardButton.Yes:
                try:
                    sessoes.encerrar_sessao_por_id(sid)
                    janela = self.window()
                    if hasattr(janela, "atualizar_dados"):
                        janela.atualizar_dados()
                except Exception as exc:
                    QMessageBox.critical(self, "Erro", str(exc))


    class JanelaPrincipal(QMainWindow):
        """Janela principal da Central de Operacoes do Catamara em PyQt6."""

        def __init__(self) -> None:
            super().__init__()
            self.setWindowTitle("Catamarã · Central de Tarefas")
            self.setMinimumSize(920, 620)
            self.resize(1080, 720)

            # Aplica paleta Dark oficial da Jangada
            self.setStyleSheet(f"""
                QMainWindow {{ background-color: {COR_SUPERFICIE}; }}
                QWidget {{ color: {COR_TEXTO}; font-family: Inter, system-ui, sans-serif; }}
                QTabWidget::pane {{ border: 1px solid {COR_BORDA}; background: {COR_SUPERFICIE}; border-radius: 8px; }}
                QTabBar::tab {{
                    background: {COR_ELEVADA};
                    color: {COR_TEXTO_SUAVE};
                    padding: 8px 18px;
                    border-top-left-radius: 6px;
                    border-top-right-radius: 6px;
                    margin-right: 4px;
                    font-weight: 600;
                    font-size: 12px;
                }}
                QTabBar::tab:selected {{
                    background: {COR_SUPERFICIE};
                    color: {COR_PRIMARIA};
                    border: 1px solid {COR_BORDA};
                    border-bottom: 2px solid {COR_PRIMARIA};
                }}
                QTabBar::tab:hover:!selected {{
                    background: {COR_ELEVADA_HOVER};
                    color: {COR_TEXTO};
                }}
            """)

            # Carrega logo oficial SVG como icone
            caminho_logo = RAIZ_CATAMARA / "core" / "painel" / "catamara-symbolic.svg"
            if caminho_logo.is_file():
                self.setWindowIcon(QIcon(str(caminho_logo)))

            self.montar_ui()
            self.configurar_timer()
            self.atualizar_dados()

        def montar_ui(self) -> None:
            central = QWidget()
            self.setCentralWidget(central)
            lay_raiz = QVBoxLayout(central)
            lay_raiz.setContentsMargins(14, 14, 14, 14)
            lay_raiz.setSpacing(10)

            # Cabecalho oficial com identificacao e botao de destaque
            topo = QHBoxLayout()
            caminho_logo = RAIZ_CATAMARA / "core" / "painel" / "catamara-symbolic.svg"
            lbl_logo = QLabel()
            lbl_logo.setFixedSize(28, 28)
            logo_carregada = False
            if caminho_logo.is_file():
                try:
                    conteudo_svg = caminho_logo.read_text(encoding="utf-8")
                    svg_colorido = conteudo_svg.replace("currentColor", COR_PRIMARIA).encode("utf-8")
                    pixmap_logo = QPixmap()
                    if pixmap_logo.loadFromData(svg_colorido):
                        lbl_logo.setPixmap(
                            pixmap_logo.scaled(
                                28,
                                28,
                                Qt.AspectRatioMode.KeepAspectRatio,
                                Qt.TransformationMode.SmoothTransformation,
                            )
                        )
                        logo_carregada = True
                except Exception:
                    pass
            if not logo_carregada:
                lbl_logo.setText("⛵")
                lbl_logo.setStyleSheet(f"font-size: 20px; color: {COR_PRIMARIA};")
            topo.addWidget(lbl_logo)

            lbl_app = QLabel("Catamarã · Central de Tarefas")
            lbl_app.setStyleSheet(f"font-size: 18px; font-weight: bold; color: {COR_TEXTO};")
            topo.addWidget(lbl_app)
            topo.addStretch()

            # Botao de destaque primario: + Nova Tarefa
            btn_nova_tarefa_topo = QPushButton("+ Nova Tarefa")
            btn_nova_tarefa_topo.setStyleSheet(f"""
                QPushButton {{
                    background-color: {COR_PRIMARIA};
                    color: {COR_TEXTO_PRIMARIO};
                    font-weight: bold;
                    font-size: 12px;
                    border: none;
                    border-radius: 6px;
                    padding: 6px 16px;
                }}
                QPushButton:hover {{
                    background-color: #b5ddfa;
                }}
            """)
            btn_nova_tarefa_topo.clicked.connect(self.abrir_dialogo_nova_tarefa)
            topo.addWidget(btn_nova_tarefa_topo)

            lbl_atalhos = QLabel("Alt+1 Tarefas │ Alt+2 Entregas │ Alt+3 Validação │ Alt+4 Indicadores │ Alt+5 Frota │ Alt+6 Sandboxes")
            lbl_atalhos.setStyleSheet(f"color: {COR_TEXTO_SUAVE}; font-size: 11px;")
            topo.addWidget(lbl_atalhos)
            lay_raiz.addLayout(topo)

            # Barra superior de telemetria rapida (estilo Waybar)
            lay_telemetria = QHBoxLayout()
            lay_telemetria.setSpacing(8)

            def criar_badge_topo(texto_ini: str) -> QLabel:
                lbl = QLabel(texto_ini)
                lbl.setStyleSheet(f"""
                    background-color: {COR_ELEVADA};
                    color: {COR_TEXTO_SUAVE};
                    border: 1px solid {COR_BORDA};
                    border-radius: 999px;
                    padding: 3px 10px;
                    font-size: 11px;
                    font-family: monospace;
                """)
                return lbl

            self.lbl_topo_c1 = criar_badge_topo("C1: -- (Argon)")
            self.lbl_topo_c2 = criar_badge_topo("C2: -- (Flash)")
            self.lbl_topo_ollama = criar_badge_topo("Ollama: VRAM")
            self.lbl_topo_portao = criar_badge_topo("Portão: OK")
            self.lbl_topo_tarefas = criar_badge_topo("0 ativas")
            self.lbl_topo_entregas = criar_badge_topo("0 entregas")

            lay_telemetria.addWidget(self.lbl_topo_c1)
            lay_telemetria.addWidget(self.lbl_topo_c2)
            lay_telemetria.addWidget(self.lbl_topo_ollama)
            lay_telemetria.addWidget(self.lbl_topo_portao)
            lay_telemetria.addWidget(self.lbl_topo_tarefas)
            lay_telemetria.addWidget(self.lbl_topo_entregas)
            lay_telemetria.addStretch()

            btn_portao_rapido = QPushButton("▶ Portão Local")
            btn_portao_rapido.setStyleSheet(f"""
                QPushButton {{
                    background-color: {COR_ELEVADA};
                    color: {COR_PRIMARIA};
                    border: 1px solid {COR_BORDA};
                    border-radius: 6px;
                    padding: 3px 10px;
                    font-size: 11px;
                    font-weight: bold;
                }}
                QPushButton:hover {{ background-color: {COR_ELEVADA_HOVER}; }}
            """)
            btn_portao_rapido.clicked.connect(lambda: self.aba_validacao.disparar_validacao(so_deterministico=True))
            lay_telemetria.addWidget(btn_portao_rapido)

            lay_raiz.addLayout(lay_telemetria)

            # Abas principais (sem & para evitar mnemônicos feios de underscore)
            self.tabs = QTabWidget()
            self.aba_tarefas = AbaTarefas()
            self.aba_entregas = AbaEntregas()
            self.aba_validacao = AbaValidacao()
            self.aba_indicadores = AbaIndicadores()
            self.aba_fleet = AbaFleetUsage()
            self.aba_jail = AbaJail()

            self.tabs.addTab(self.aba_tarefas, "Tarefas e Projetos")
            self.tabs.addTab(self.aba_entregas, "Entregas e Memória")
            self.tabs.addTab(self.aba_validacao, "Validação Cruzada")
            self.tabs.addTab(self.aba_indicadores, "Indicadores de Frota")
            self.tabs.addTab(self.aba_fleet, "Frota e Cotas")
            self.tabs.addTab(self.aba_jail, "Sandboxes e Isolamento")

            lay_raiz.addWidget(self.tabs)

            # Barra de status inferior
            self.lbl_status = QLabel("Carregando telemetria...")
            self.lbl_status.setStyleSheet(f"color: {COR_TEXTO_SUAVE}; font-size: 11px; padding-top: 4px;")
            lay_raiz.addWidget(self.lbl_status)

            # Atalhos de navegacao
            QShortcut(QKeySequence("Alt+1"), self, lambda: self.tabs.setCurrentIndex(0))
            QShortcut(QKeySequence("Alt+2"), self, lambda: self.tabs.setCurrentIndex(1))
            QShortcut(QKeySequence("Alt+3"), self, lambda: self.tabs.setCurrentIndex(2))
            QShortcut(QKeySequence("Alt+4"), self, lambda: self.tabs.setCurrentIndex(3))
            QShortcut(QKeySequence("Alt+5"), self, lambda: self.tabs.setCurrentIndex(4))
            QShortcut(QKeySequence("Alt+6"), self, lambda: self.tabs.setCurrentIndex(5))
            QShortcut(QKeySequence("Alt+N"), self, self.abrir_dialogo_nova_tarefa)
            QShortcut(QKeySequence("Alt+A"), self, self.aba_entregas.ao_clicar_aceitar)

            self.aba_tarefas.btn_atualizar.clicked.connect(self.atualizar_dados)
            self.aba_fleet.btn_atualizar.clicked.connect(self.atualizar_dados)
            self.aba_indicadores.btn_atualizar.clicked.connect(self.atualizar_dados)

        def abrir_dialogo_nova_tarefa(self) -> None:
            dlg = DialogoNovaTarefa(self)
            if dlg.exec() == QDialog.DialogCode.Accepted:
                self.atualizar_dados()

        def configurar_timer(self) -> None:
            self.timer = QTimer(self)
            self.timer.timeout.connect(self.atualizar_dados)
            self.timer.start(2500)

        def atualizar_dados(self) -> None:
            try:
                # 1. Carrega tarefas / sessoes
                pasta_ses = obter_pasta_estado() / "sessoes"
                sessoes_lista: list[dict[str, Any]] = []
                sessoes_ativas_qtd = 0
                if pasta_ses.is_dir():
                    for arq in sorted(pasta_ses.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
                        try:
                            d = json.loads(arq.read_text(encoding="utf-8"))
                            sid = d.get("sessao") or arq.stem
                            tmux_id = d.get("tmux") or sid
                            ativa = sessoes.tmux_has_session("catamara", tmux_id)
                            d["ativa_no_tmux"] = ativa
                            if ativa or d.get("estado") == "ativa":
                                sessoes_ativas_qtd += 1
                            sessoes_lista.append(d)
                        except Exception:
                            continue
                self.aba_tarefas.atualizar_sessoes(sessoes_lista)

                # 2. Carrega cascos
                lista_cascos = contas.catalogo_cascos()
                self.aba_fleet.atualizar_cascos(lista_cascos)

                # Atualiza badges superiores de cascos
                for c in lista_cascos:
                    cotas = c.get("cotas", {})
                    nome = c.get("nome", "")
                    if "1" in nome or c.get("conta") == "conta1":
                        arg = cotas.get("argon_pct")
                        self.lbl_topo_c1.setText(f"C1: {int(arg)}% (Argon)" if arg is not None else "C1: -- (Argon)")
                    elif "2" in nome or c.get("conta") == "conta2":
                        fla = cotas.get("gemini_pct")
                        self.lbl_topo_c2.setText(f"C2: {int(fla)}% (Flash)" if fla is not None else "C2: -- (Flash)")

                # 3. Carrega processos confinados (AI Jail)
                pasta_proc = obter_pasta_estado() / "processos"
                procs: list[dict[str, Any]] = []
                if pasta_proc.is_dir():
                    for arq in sorted(pasta_proc.glob("*.json")):
                        try:
                            d = json.loads(arq.read_text(encoding="utf-8"))
                            pid = d.get("pid")
                            if pid and not Path(f"/proc/{pid}").exists():
                                arq.unlink(missing_ok=True)
                                continue
                            procs.append(d)
                        except Exception:
                            continue
                self.aba_jail.atualizar_processos(procs)

                # 4. Carrega entregas delimitadas (AI Memory)
                pasta_impl = obter_pasta_estado() / "implementacoes"
                entregas_lista: list[dict[str, Any]] = []
                pendentes_qtd = 0
                if pasta_impl.is_dir():
                    for sub in sorted(pasta_impl.glob("impl-*"), key=lambda p: p.name, reverse=True):
                        manif = sub / "manifesto.json"
                        ent = sub / "entrega.json"
                        if manif.is_file():
                            try:
                                d_m = json.loads(manif.read_text(encoding="utf-8"))
                                d_e = json.loads(ent.read_text(encoding="utf-8")) if ent.is_file() else None
                                ja_aceito = bool(d_e and d_e.get("aceito") is True)
                                pendente = (d_m.get("estado") == "finalizado") and not ja_aceito
                                if pendente:
                                    pendentes_qtd += 1
                                entregas_lista.append({
                                    "id": d_m.get("id") or sub.name,
                                    "estado": "aceito" if ja_aceito else d_m.get("estado", ""),
                                    "base": d_m.get("base", ""),
                                    "tarefa": d_m.get("tarefa", ""),
                                    "pendente": pendente,
                                    "entrega": d_e,
                                })
                            except Exception:
                                continue
                self.aba_entregas.atualizar_entregas(entregas_lista)

                # 5. Carrega validacao e historico
                recibos_val: list[dict[str, Any]] = []
                val_jsonl = obter_pasta_estado() / "validar.jsonl"
                if val_jsonl.is_file():
                    try:
                        linhas = [json.loads(l) for l in val_jsonl.read_text(encoding="utf-8").strip().splitlines() if l.strip()]
                        recibos_val = list(reversed(linhas))
                    except Exception:
                        pass
                self.aba_validacao.atualizar_validacao(recibos_val)

                # 6. Carrega indicadores e journal
                ind_dados: dict[str, Any] = {
                    "economia_motores": {"cota_poupada_pct": 0, "por_motor": {}},
                    "balanco_cascos": {"conta1_chamadas": 0, "conta2_chamadas": 0},
                    "funil_qualidade": {"taxa_deterministico_pct": 100, "taxa_revisao_pct": 100, "implementacoes_aceitas": 0, "implementacoes_pendentes": pendentes_qtd},
                    "linha_do_tempo": [],
                }
                eventos_jsonl = obter_pasta_estado() / "eventos.jsonl"
                if eventos_jsonl.is_file():
                    try:
                        ev_linhas = [json.loads(l) for l in eventos_jsonl.read_text(encoding="utf-8").strip().splitlines() if l.strip()]
                        ind_dados["linha_do_tempo"] = list(reversed(ev_linhas[-20:]))
                    except Exception:
                        pass
                self.aba_indicadores.atualizar_indicadores(ind_dados)

                # Atualiza badges superiores restantes
                self.lbl_topo_tarefas.setText(f"{sessoes_ativas_qtd} ativa" if sessoes_ativas_qtd == 1 else f"{sessoes_ativas_qtd} ativas")
                self.lbl_topo_entregas.setText(f"{pendentes_qtd} pendente" if pendentes_qtd == 1 else f"{pendentes_qtd} pendentes")
                if pendentes_qtd > 0:
                    self.lbl_topo_entregas.setStyleSheet(f"background-color: #3d2f1c; color: {COR_BLOQUEADA}; border: 1px solid rgba(242, 198, 109, 0.4); border-radius: 999px; padding: 3px 10px; font-size: 11px; font-family: monospace; font-weight: bold;")
                else:
                    self.lbl_topo_entregas.setStyleSheet(f"background-color: {COR_ELEVADA}; color: {COR_TEXTO_SUAVE}; border: 1px solid {COR_BORDA}; border-radius: 999px; padding: 3px 10px; font-size: 11px; font-family: monospace;")

                if recibos_val:
                    portao_ok = recibos_val[0].get("deterministico_ok") is True
                    self.lbl_topo_portao.setText("Portão: OK" if portao_ok else "Portão: FALHA")
                    self.lbl_topo_portao.setStyleSheet(f"background-color: {'#1c2e24' if portao_ok else '#2e1c1c'}; color: {COR_CONCLUIDA if portao_ok else COR_FALHOU}; border: 1px solid {COR_BORDA}; border-radius: 999px; padding: 3px 10px; font-size: 11px; font-family: monospace; font-weight: bold;")

                agora_str = time.strftime("%H:%M:%S")
                self.lbl_status.setText(f"Telemetria atualizada às {agora_str} │ {len(sessoes_lista)} tarefa(s) │ {len(lista_cascos)} cascos │ {pendentes_qtd} entrega(s) pendente(s) │ {len(procs)} sandbox(es)")
            except Exception as exc:
                self.lbl_status.setText(f"Erro ao atualizar telemetria: {exc}")


    if WEBENGINE_DISPONIVEL:

        class JanelaCentralWebQt(QMainWindow):
            """Janela principal da Central de Operacoes do Catamara unificada via QWebEngineView."""

            def __init__(self, url_servidor: str) -> None:
                super().__init__()
                self.url_servidor = url_servidor
                self.setWindowTitle("Catamarã · Central de Tarefas")
                self.setMinimumSize(960, 620)
                self.resize(1380, 880)

                # Aplica paleta Dark oficial da Jangada
                self.setStyleSheet(f"""
                    QMainWindow {{ background-color: {COR_SUPERFICIE}; }}
                    QWidget {{ background-color: {COR_SUPERFICIE}; color: {COR_TEXTO}; }}
                """)

                # Carrega logo oficial SVG como icone
                caminho_logo = RAIZ_CATAMARA / "core" / "painel" / "catamara-symbolic.svg"
                if caminho_logo.is_file():
                    self.setWindowIcon(QIcon(str(caminho_logo)))

                self.montar_ui()

            def montar_ui(self) -> None:
                self.view = QWebEngineView(self)
                self.view.setUrl(QUrl(self.url_servidor))
                self.setCentralWidget(self.view)

                # Atalhos oficiais: F5/Ctrl+R recarrega, F11 tela cheia
                QShortcut(QKeySequence("F5"), self, self.view.reload)
                QShortcut(QKeySequence("Ctrl+R"), self, self.view.reload)
                QShortcut(QKeySequence("F11"), self, self.alternar_tela_cheia)

            def alternar_tela_cheia(self) -> None:
                if self.isFullScreen():
                    self.showNormal()
                else:
                    self.showFullScreen()
    else:

        class JanelaCentralWebQt:  # type: ignore[no-redef]
            pass


def testar_porta_ativa(host: str, porta: int, timeout: float = 0.3) -> bool:
    try:
        with socket.create_connection((host, porta), timeout=timeout):
            return True
    except (OSError, ConnectionRefusedError):
        return False


def garantir_servidor_ativo(porta: int = 8770) -> str:
    host = "127.0.0.1"
    url = f"http://{host}:{porta}"
    if testar_porta_ativa(host, porta):
        return url

    script_servidor = RAIZ_CATAMARA / "core" / "painel" / "servidor.py"
    if script_servidor.is_file():
        cmd = [sys.executable, str(script_servidor), "--porta", str(porta)]
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)

        for _ in range(30):
            time.sleep(0.1)
            if testar_porta_ativa(host, porta):
                return url

    return url


def iniciar_central_desktop(porta: int = 8770, modo_webengine: bool = False, apenas_browser: bool = False) -> int:
    """Inicia a aplicacao Qt ou foca a instancia existente via soquete UNIX."""
    url = garantir_servidor_ativo(porta)

    if apenas_browser:
        print(f"Abrindo Central de Tarefas no navegador: {url}")
        webbrowser.open(url)
        return 0

    if not PYQT_DISPONIVEL:
        print("Aviso: PyQt6 nao esta instalado no ambiente Python.", file=sys.stderr)
        print(f"Abrindo Central de Tarefas no navegador padrao: {url}")
        webbrowser.open(url)
        return 0

    app = QApplication.instance() or QApplication(sys.argv)
    app.setStyle("Fusion")

    # Configura Paleta Dark oficial da Jangada para impedir superficies brancas
    paleta = QPalette()
    paleta.setColor(QPalette.ColorRole.Window, QColor(COR_SUPERFICIE))
    paleta.setColor(QPalette.ColorRole.WindowText, QColor(COR_TEXTO))
    paleta.setColor(QPalette.ColorRole.Base, QColor(COR_ELEVADA))
    paleta.setColor(QPalette.ColorRole.AlternateBase, QColor(COR_SUPERFICIE))
    paleta.setColor(QPalette.ColorRole.ToolTipBase, QColor(COR_ELEVADA))
    paleta.setColor(QPalette.ColorRole.ToolTipText, QColor(COR_TEXTO))
    paleta.setColor(QPalette.ColorRole.Text, QColor(COR_TEXTO))
    paleta.setColor(QPalette.ColorRole.Button, QColor(COR_ELEVADA))
    paleta.setColor(QPalette.ColorRole.ButtonText, QColor(COR_TEXTO))
    paleta.setColor(QPalette.ColorRole.BrightText, QColor(COR_PRIMARIA))
    paleta.setColor(QPalette.ColorRole.Link, QColor(COR_PRIMARIA))
    paleta.setColor(QPalette.ColorRole.Highlight, QColor("#1f4968"))
    paleta.setColor(QPalette.ColorRole.HighlightedText, QColor("#ffffff"))
    app.setPalette(paleta)

    app.setStyleSheet(f"""
        QMainWindow, QWidget, QDialog {{
            background-color: {COR_SUPERFICIE};
            color: {COR_TEXTO};
            font-family: Inter, system-ui, sans-serif;
        }}
        QScrollArea, QScrollArea > QWidget, QScrollArea > QWidget > QWidget {{
            background-color: {COR_SUPERFICIE};
        }}
        QTabWidget::pane {{
            border: 1px solid {COR_BORDA};
            background: {COR_SUPERFICIE};
            border-radius: 6px;
        }}
        QTabBar::tab {{
            background: {COR_ELEVADA};
            color: {COR_TEXTO_SUAVE};
            padding: 8px 16px;
            border-top-left-radius: 6px;
            border-top-right-radius: 6px;
            margin-right: 4px;
            font-weight: bold;
        }}
        QTabBar::tab:selected {{
            background: {COR_SUPERFICIE};
            color: {COR_PRIMARIA};
            border: 1px solid {COR_BORDA};
            border-bottom: 1px solid {COR_SUPERFICIE};
        }}
        QTableWidget, QTextEdit {{
            background-color: {COR_ELEVADA};
            color: {COR_TEXTO};
            border: 1px solid {COR_BORDA};
            border-radius: 6px;
            selection-background-color: #1f4968;
            selection-color: #ffffff;
        }}
        QHeaderView::section {{
            background-color: #262b30;
            color: {COR_PRIMARIA};
            font-weight: bold;
            padding: 5px;
            border: 1px solid rgba(193, 199, 206, 0.12);
        }}
        QScrollBar:vertical, QScrollBar:horizontal {{
            background: {COR_SUPERFICIE};
            width: 8px;
            height: 8px;
            margin: 0px;
        }}
        QScrollBar::handle:vertical, QScrollBar::handle:horizontal {{
            background: #262b30;
            min-height: 20px;
            border-radius: 4px;
        }}
        QSplitter::handle {{
            background-color: {COR_BORDA};
        }}
    """)

    sock_path = caminho_soquete_servidor()

    # Tenta conectar a uma instancia existente
    cliente_socket = QLocalSocket()
    cliente_socket.connectToServer(sock_path)
    if cliente_socket.waitForConnected(300):
        cliente_socket.write(b"mostrar\n")
        cliente_socket.waitForBytesWritten(300)
        cliente_socket.disconnectFromServer()
        # Notifica Niri ou Hyprland para focar na janela existente
        if os.environ.get("NIRI_SOCKET"):
            subprocess.run(["niri", "msg", "action", "focus-window", "--matching-title", "Catamarã"], capture_output=True, check=False)
        elif os.environ.get("HYPRLAND_INSTANCE_SIGNATURE"):
            subprocess.run(["hyprctl", "dispatch", "focuswindow", "Catamarã"], capture_output=True, check=False)
        return 0

    # Cria servidor de soquete para garantir instancia unica
    Path(sock_path).unlink(missing_ok=True)
    servidor_socket = QLocalServer()
    servidor_socket.listen(sock_path)

    if modo_webengine and WEBENGINE_DISPONIVEL:
        janela = JanelaCentralWebQt(url)
    else:
        janela = JanelaPrincipal()

    def ao_receber_conexao() -> None:
        while servidor_socket.hasPendingConnections():
            sock = servidor_socket.nextPendingConnection()
            if sock:
                janela.showNormal()
                janela.raise_()
                janela.activateWindow()
                if os.environ.get("NIRI_SOCKET"):
                    subprocess.run(["niri", "msg", "action", "focus-window", "--matching-title", "Catamarã"], capture_output=True, check=False)
                elif os.environ.get("HYPRLAND_INSTANCE_SIGNATURE"):
                    subprocess.run(["hyprctl", "dispatch", "focuswindow", f"pid:{os.getpid()}"], capture_output=True, check=False)
                sock.disconnectFromServer()

    servidor_socket.newConnection.connect(ao_receber_conexao)

    janela.show()
    return app.exec()


def main() -> int:
    parser = argparse.ArgumentParser(description="Central de Tarefas Desktop do Catamara em PyQt6")
    parser.add_argument(
        "--porta",
        type=int,
        default=int(os.environ.get("CATAMARA_PAINEL_PORTA", 8770)),
        help="Porta TCP do servidor HTTP local (padrao: 8770)",
    )
    parser.add_argument(
        "--webengine",
        action="store_true",
        help="Usa a interface experimental baseada em WebEngine em vez dos widgets nativos da frota",
    )
    parser.add_argument(
        "--web",
        "--browser",
        action="store_true",
        help="Abre a Central diretamente no navegador padrao",
    )
    args = parser.parse_args()
    return iniciar_central_desktop(porta=args.porta, modo_webengine=args.webengine, apenas_browser=args.web)


if __name__ == "__main__":
    sys.exit(main())
