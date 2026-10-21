# Catamarã (`catamara` / `cata`)

```text
                |                  JANGADA · CATAMARÃ
                |\                 Frota Multiagente N-Cascos
                | \                Coordenador Argon + Executor Flash + Revisor Flexível + Motor Local
                |  \               
                | (*) \            • Casco 1 (conta1): Sessão Principal / Coordenador (Argon ativo)
                |      \           • Casco 2 (conta2): Execução Paralela / Especialistas (Sem Argon)
                |_______\          • Frota N-Cascos:   Claude Code, Codex CLI, Agy isolado e Ollama
     ___________|____________      
     \==|==|==|==|==|==|==|=/      
   ~~~~  ~~~~~~   ~~~~~~  ~~~~     
```

O **Catamarã** é uma aplicação multiagente enxuta e independente da família `jangada`, construída para orquestrar uma frota flexível de N cascos heterogêneos (Antigravity CLI com N contas isoladas, Claude Code CLI, Codex CLI e motor local Ollama). A arquitetura opera sob confinamento estrito do kernel (`bubblewrap`), controle determinístico de integridade por hash SHA-256 e interface visual inspirada nos conceitos do `ai-toolkit-web` (AI Usage Bar, AI Jail e AI Memory).

---

## 1. As 4 Camadas da Arquitetura

O software distribui a carga de trabalho em quatro camadas especializadas para preservar a cota semanal do modelo mais forte (`Gemini 4 Argon`), acelerar tarefas delimitadas com `Gemini 3.8 Flash`, manter dados sensíveis no `Ollama` local e permitir revisão independente com múltiplos provedores (`Claude`, `Codex` ou conta oposta do `Agy`):

```mermaid
flowchart TB
    subgraph C0["Interface e Controle (CLI cata + Central Desktop Qt / Web)"]
        UI["Central de Tarefas Desktop Qt e Web<br>(bin/catamara-tarefas-qt + core/painel/central_qt.py)"]
        CLI["CLI Unificado (bin/catamara e bin/cata)<br>orient | doctor | conta | isolar | delegar | validar | tarefas"]
    end

    subgraph C1["Camada 1: Coordenador (Sessão Principal)"]
        COORD["Coordenador: Gemini 4 Argon (Medium / High)<br>git worktree + tmux -L catamara + bwrap<br>Planeja, divide tarefas, inspeciona diffs e aprova por hash SHA-256"]
    end

    subgraph C2["Camada 2: Execução Delimitada e Especialistas Rápidos (Flash)"]
        IMPL["Implementador Confinado (gemini-3.8-flash-high)<br>Cópia isolada sem .git, escrita restrita a --permitidos<br>Gera entrega.json + hashes SHA-256"]
        ESP["Especialistas de Leitura (gemini-3.8-flash-high)<br>explorador | pesquisador | arquiteto | auditor | otimizador | verificador | supervisor<br>Balanceados entre Casco 1 (conta1) e Casco 2 (conta2)"]
    end

    subgraph C3["Camada 3: Leitura e Redação Local (Ollama)"]
        OL["Motor Local: qwen3:4b (Avaliação de VRAM e Jogo Steam)<br>leitor (temp 0.0, faixas ARQ:INI-FIM, pdftotext, extração tipada)<br>redator (temp 0.2) + Consolidação determinística (extracao.py)"]
    end

    subgraph C4["Camada 4: Validação Independente (Portão Local + Revisão Flexível)"]
        DET["1. Portão Determinístico Local (sem rede)<br>Conflitos Git, bash -n, py_compile, segredos/gitleaks e testes"]
        REV["2. Revisor Flexível em Cascata (jangada-validar)<br>Claude Code CLI / Codex CLI / Agy conta oposta<br>Pareceres em markdown + token incremental .aprovado"]
    end

    UI --> CLI
    CLI --> COORD
    COORD -->|"leitor / redator"| OL
    COORD -->|"especialistas"| ESP
    COORD -->|"implementador"| IMPL
    IMPL -->|"entrega.json + SHA-256"| COORD
    COORD -->|"catamara validar"| DET
    DET -->|"local aprovado"| REV
```

---

## 2. Conceitos do AI-Toolkit-Web Incorporados

Inspirado nas soluções de observabilidade e segurança do `ai-toolkit-web` (Akita on Rails), o Catamarã adota três pilares arquiteturais:

1. **AI Usage Bar (Indicadores em Tempo Real):**
   - Telemetria de cotas para toda a frota (Claude, Codex, Agy 1..N e Ollama).
   - **Fidelidade estrita de cotas:** o Casco 2 não possui o balde semanal de Argon (`gemini-argon-wtu-weekly`). O sistema identifica essa ausência com precisão, marcando `tem_argon: false`, zerando a exibição de cota falsa e exibindo o badge visual `[Sem Argon]`.

2. **AI Jail (Confinamento Estrito e Telemetria):**
   - Isolamento via `bubblewrap` com mascaramento recíproco de credenciais (`bin/catamara-isolar`). Ao rodar tarefas do Agy, credenciais de Claude (`~/.claude`) e Codex (`~/.codex`) são ocultadas via tmpfs, além de limpar variáveis de ambiente cruzadas (`ANTHROPIC_*`, `OPENAI_*`).
   - Telemetria de processos confinados gravada em `$CATAMARA_ESTADO/processos/` para auditoria em tempo real.

3. **AI Memory e Gestão de Entregas:**
   - Inspeção de diffs na interface Qt com realce de sintaxe em tipografia `JetBrains Mono`.
   - Aceite atômico de alterações com conferência de hash SHA-256, garantindo que nada seja aplicado ao código sem auditoria prévia.

---

## 3. Ciclo de Vida Completo de uma Tarefa

O diagrama abaixo mostra o percurso de uma tarefa desde a abertura da sessão até a aprovação final na validação cruzada:

```mermaid
sequenceDiagram
    autonumber
    actor U as Usuário
    participant C as Coordenador Argon (Conta 1)
    participant O as Ollama Local (qwen3:4b)
    participant F as Implementador Flash (Conta 2)
    participant D as Portão Determinístico
    participant R as Revisor Flexível (Claude / Codex / Agy)

    U->>C: cata iniciar --sessao s-feature
    C->>C: cata orient --json (lê repo, cotas da frota e VRAM)
    
    alt Leitura de documentos ou extração estruturada
        C->>O: cata delegar leitor --extrair campo:tipo "Tarefa" doc.pdf
        O-->>C: JSON consolidado deterministicamente (extracao.py)
    end

    alt Exploração ou análise de arquitetura/segurança
        C->>F: cata delegar arquiteto "Analisar impacto" core/
        F-->>C: Relatório técnico com caminho:linha
    end

    C->>F: cata delegar implementador --base SHA --arquivos A,B --permitidos A --criterio "..." "Tarefa"
    Note over F: Roda em workspace isolado sem .git<br>Escrita restrita a --permitidos
    F-->>C: entrega.json (entrega_hash + diff + hashes SHA-256)

    C->>C: Inspeciona diff e roda testes locais
    C->>F: cata delegar implementador --aceitar ID --entrega HASH
    Note over C: Aplica arquivos atomicamente se a base SHA coincidir

    C->>D: cata validar
    D-->>C: Conflitos, sintaxe, segredos e testes OK
    D->>R: Congela foto somente leitura e aciona revisor escolhido
    Note over R: Claude Code CLI -> Agy Conta Oposta -> Codex CLI<br>Veredito estrito: STATUS: APROVADO / REVISAR
    R-->>U: Parecer gravado em pareceres/ e token .aprovado
```

---

## 4. Isolamento Multi-Conta, Multi-Provedor e Frota de Cascos

O `catamara-isolar` impede que processos acessem tokens indevidos ou credenciais de outros provedores:

```mermaid
flowchart LR
    subgraph Host["Estado no Host (~/.local/state/catamara)"]
        C1["contas/conta1/antigravity-cli/<br>antigravity-oauth-token"]
        C2["contas/conta2/antigravity-cli/<br>antigravity-oauth-token (Sem Argon)"]
        COTAS["cotas/conta1.json<br>cotas/conta2.json"]
        CLAUDE_CREDS["~/.claude/"]
        CODEX_CREDS["~/.codex/"]
    end

    subgraph Sandbox["Interior do Bubblewrap (catamara-isolar)"]
        TMP_CONTAS["--tmpfs ~/.local/state/catamara/contas<br>(oculta tokens das outras contas)"]
        TMP_CROSS["--tmpfs ~/.claude e ~/.codex<br>(bloqueia credenciais cruzadas)"]
        BIND_ATIVA["--bind conta_escolhida -> ~/.gemini/antigravity-cli<br>(somente a conta ativa fica visível)"]
        DBUS["xdg-dbus-proxy filtrado<br>Permite: Notifications<br>Bloqueia: secrets"]
    end

    C1 -.->|"quando --conta conta1"| BIND_ATIVA
    C2 -.->|"quando --conta conta2"| BIND_ATIVA
    TMP_CONTAS --> BIND_ATIVA
    TMP_CROSS --> Sandbox
```

### Frota de Cascos e Regras de Roteamento (`core/contas/contas.py`)

| Casco / Provedor | Identificador | Capacidades e Papéis | Política de Cota e Fallback |
|---|---|---|---|
| **Agy Casco 1** | `conta1` | Coordenador (`autor`), Especialistas | Possui Argon semanal ativo (`tem_argon: true`). Usado prioritariamente para coordenação. |
| **Agy Casco 2** | `conta2` | Executor Flash (`executor`), Especialistas | Sem Argon (`tem_argon: false`, cota 0%). Não aceita papéis de Argon. Balanceia tarefas Flash. |
| **Claude Code** | `claude` | Revisor (`revisor`), Análise Profunda | Invoca CLI Claude oficial sob isolamento. Usado na primeira linha de revisão independente. |
| **Codex CLI** | `codex` | Revisor alternativo, Implementador | CLI de apoio em cascata para tarefas de código e validação secundária. |
| **Ollama Local** | `local` | Leitor (`leitor`), Redator (`redator`) | Modelo `qwen3:4b`. Exige VRAM >= 4000 MiB e ausência de jogos Steam. Trava via `flock`. |

---

## 5. Estrutura do Repositório

```text
catamara/
├── bin/
│   ├── catamara               CLI principal unificado
│   ├── cata                   Atalho curto para bin/catamara
│   ├── catamara-config        Variáveis XDG, modelos padrão, travas flock e git seguro
│   ├── catamara-isolar        Sandbox bubblewrap com isolamento multi-provedor e telemetria
│   ├── catamara-delegar       Roteador para Ollama local, especialistas Flash e implementador
│   ├── catamara-validar       Validador flexível estilo jangada-validar (Claude, Codex, Agy)
│   ├── catamara-hook-leitor   Filtro PreToolUse estrito para comandos de leitura
│   ├── catamara-tarefas       Central de Tarefas CLI, TUI, Web e Waybar
│   └── catamara-tarefas-qt    Central Desktop nativa PyQt6 com AI Usage Bar e AI Memory
├── core/
│   ├── contas/
│   │   └── contas.py          Frota N-cascos, catálogo, fidelidade Argon e fallback
│   ├── delegacao/
│   │   ├── avaliar_ollama.py  Checagem de VRAM/Steam e execução local no qwen3:4b
│   │   ├── extracao.py        Esquema tipado e consolidação determinística de fatias
│   │   └── implementacao.py   Preparação, montagem bwrap, finalização e aceite por SHA-256
│   ├── orquestracao/
│   │   ├── orient.py          Fotografia rápida do repositório, sessão, contas e Ollama
│   │   ├── doctor.py          Diagnóstico de binários, isolamento bwrap e sincronização
│   │   ├── deterministico.py  Verificador offline de conflitos, sintaxe, segredos e testes
│   │   └── sessoes.py         Ciclo de vida de sessões tmux -L catamara e git worktree
│   ├── painel/
│   │   ├── servidor.py        API HTTP local (127.0.0.1) e coletor de estado da frota
│   │   ├── central.html       Interface Web oficial unificada com identidade Jangada
│   │   ├── central_qt.py      Container Desktop nativo PyQt6 (QWebEngineView + fallback)
│   │   └── catamara-symbolic.svg  Logotipo oficial preservado
│   └── agentes/
│       └── agy/
│           ├── agents/        11 especialistas (.md): explorador, leitor, pesquisador, arquiteto,
│           │                  auditor, otimizador, redator, verificador, supervisor, implementador, revisor
│           └── hooks.json     Configuração do filtro PreToolUse
└── testes/
    └── verificar.sh           Suíte determinística completa com checagem de regras e integridade
```

---

## 6. Comandos Principais

```bash
# 1. Listar catálogo da frota de cascos e status de contas
./bin/cata conta cascos
./bin/cata conta status

# 2. Inicializar contas Agy e efetuar login do Casco 2
./bin/cata conta init --importar-atual
./bin/cata conta login conta2

# 3. Diagnóstico de integridade e orientação da sessão
./bin/cata doctor
./bin/cata orient

# 4. Central de Tarefas Unificada (Desktop PyQt6, Navegador Web e Terminal TUI)
./bin/cata tarefas --qt       # Janela desktop nativa unificada (QWebEngineView com central.html)
./bin/cata tarefas-qt         # Atalho direto para a janela desktop
./bin/cata tarefas --web      # Abre a interface gráfica diretamente no navegador padrão
./bin/cata tarefas --status   # Modo TUI renderizado em ASCII art no terminal
./bin/cata tarefas --qt --nativa  # Modo legado com widgets nativos QWidget (sem WebEngine)

# 5. Delegar leitura local (Ollama) ou análise por especialistas
./bin/cata delegar leitor --extrair autor:texto "Extrair metadados" artigo.pdf
./bin/cata delegar arquiteto "Avaliar acoplamento de módulos" core/

# 6. Implementação confinada e aceite por hash SHA-256
./bin/cata delegar implementador \
  --base "$(git rev-parse HEAD)" \
  --arquivos core/contas/contas.py \
  --permitidos core/contas/contas.py \
  --criterio "python3 -m py_compile core/contas/contas.py" \
  "Adicionar catálogo da frota de cascos"
./bin/cata delegar implementador --aceitar <ID> --entrega <HASH>

# 7. Validação flexível estilo jangada-validar
./bin/cata validar --revisor claude
./bin/cata validar --revisor oposto
./bin/cata validar --so-deterministico

# 8. Executar suíte de testes do projeto
./testes/verificar.sh
```
