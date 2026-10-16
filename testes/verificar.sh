#!/usr/bin/env bash
# Suite de testes e verificacao do Catamara (Fases 1, 2 e 3).
# Executa checagens sintaticas, semanticas e operacionais em ambiente temporario isolado.

set -uo pipefail

RAIZ_CATAMARA="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

TOTAL_TESTES=0
TESTES_OK=0
TESTES_FALHA=0
TESTES_PULADOS=0

registrar_resultado() {
  local nome="$1"
  local status="$2"
  local detalhe="${3:-}"
  TOTAL_TESTES=$((TOTAL_TESTES + 1))
  case "$status" in
    OK)
      TESTES_OK=$((TESTES_OK + 1))
      printf '  [OK]     %s\n' "$nome"
      ;;
    FALHA)
      TESTES_FALHA=$((TESTES_FALHA + 1))
      printf '  [FALHA]  %s (%s)\n' "$nome" "$detalhe"
      ;;
    PULADO)
      TESTES_PULADOS=$((TESTES_PULADOS + 1))
      printf '  [PULADO] %s (%s)\n' "$nome" "$detalhe"
      ;;
  esac
}

echo "=== Suite de Verificacao do Catamara ==="
echo "Raiz do projeto: $RAIZ_CATAMARA"

# 1. Checagens sintaticas fundamentais (sem ambiente isolado)
echo
echo "--- 1. Sintaxe de Scripts e Compilacao Python ---"

for script in bin/catamara-config bin/catamara-isolar bin/catamara bin/catamara-validar bin/catamara-delegar bin/catamara-hook-leitor bin/catamara-tarefas-mockup testes/verificar.sh; do
  caminho_abs="$RAIZ_CATAMARA/$script"
  if [[ -f "$caminho_abs" ]]; then
    if bash -n "$caminho_abs" 2>/dev/null; then
      registrar_resultado "bash -n $script" OK
    else
      registrar_resultado "bash -n $script" FALHA "erro de sintaxe bash"
    fi
  else
    registrar_resultado "bash -n $script" PULADO "arquivo ausente"
  fi
done

for py_mod in core/contas/contas.py core/orquestracao/orient.py core/orquestracao/doctor.py \
              core/orquestracao/deterministico.py core/orquestracao/sessoes.py \
              core/delegacao/extracao.py core/delegacao/avaliar_ollama.py core/delegacao/implementacao.py \
              core/painel/servidor.py bin/catamara-tarefas; do
  caminho_abs="$RAIZ_CATAMARA/$py_mod"
  if [[ -f "$caminho_abs" ]]; then
    if python3 -m py_compile "$caminho_abs" 2>/dev/null; then
      registrar_resultado "py_compile $py_mod" OK
    else
      registrar_resultado "py_compile $py_mod" FALHA "erro de sintaxe python"
    fi
  else
    registrar_resultado "py_compile $py_mod" PULADO "arquivo ausente"
  fi
done

# Checagem de travessoes (U+2013 e U+2014) nos arquivos criados
echo
echo "--- 2. Verificacao de Diretrizes (Ausencia de Travessoes) ---"
travessoes_detectados=0
while IFS= read -r arq; do
  if grep -P "[\x{2013}\x{2014}]" "$arq" >/dev/null 2>&1; then
    registrar_resultado "sem travessoes em $arq" FALHA "caractere U+2013 ou U+2014 encontrado"
    travessoes_detectados=$((travessoes_detectados + 1))
  fi
done < <(find "$RAIZ_CATAMARA/bin" "$RAIZ_CATAMARA/core" "$RAIZ_CATAMARA/testes" -type f \( -name "*.py" -o -name "*.sh" -o -name "catamara*" \) 2>/dev/null)

if [[ $travessoes_detectados -eq 0 ]]; then
  registrar_resultado "ausencia total de travessoes" OK
fi

# 3. Montagem do ambiente isolado temporario
echo
echo "--- 3. Preparacao de Ambiente Temporario ---"

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

export CATAMARA_PATH="$RAIZ_CATAMARA"
export CATAMARA_ESTADO="$TMP/state"
export CATAMARA_CONFIG="$TMP/config"
export CATAMARA_SESSOES="$TMP/state/sessoes"
export CATAMARA_CONTAS_DIR="$TMP/state/contas"
export CATAMARA_WORKTREES="$TMP/worktrees"
export CATAMARA_AGY_GLOBAL_DIR="$TMP/global-agy"
export CATAMARA_CORE_PY="$RAIZ_CATAMARA/core"
export CATAMARA_OLLAMA_URL="http://127.0.0.1:9"
export TMUX_TMPDIR="$TMP/tmux"

mkdir -p "$CATAMARA_ESTADO" "$CATAMARA_CONFIG" "$CATAMARA_SESSOES" "$CATAMARA_CONTAS_DIR" \
         "$CATAMARA_WORKTREES" "$CATAMARA_AGY_GLOBAL_DIR" "$TMUX_TMPDIR" "$TMP/pycache"

# Validacao de seguranca: aborta se CATAMARA_CONTAS_DIR nao estiver sob TMP
if [[ "$CATAMARA_CONTAS_DIR" != "$TMP/"* ]]; then
  echo "Erro critico de isolamento: CATAMARA_CONTAS_DIR ($CATAMARA_CONTAS_DIR) nao comeca com $TMP/" >&2
  exit 1
fi
registrar_resultado "validacao do caminho temporario CATAMARA_CONTAS_DIR" OK

# Criacao de stubs para o executavel agy
STUBS_DIR="$TMP/stubs"
mkdir -p "$STUBS_DIR"
cat <<'EOF' > "$STUBS_DIR/agy"
#!/usr/bin/env bash
if [[ "$*" =~ /usage ]]; then
  echo '{"command":{"data":{"groups":[{"buckets":[{"id":"gemini-5h","name":"gemini-5h","remaining_fraction":0.85},{"id":"gemini-weekly","name":"gemini-weekly","remaining_fraction":0.80}]}]}}}'
  exit 0
fi
echo '{"veredito":"aprovado","achados":[]}'
exit 0
EOF
chmod +x "$STUBS_DIR/agy"
export PATH="$STUBS_DIR:$PATH"

# 4. Testes de Contas e Credenciais Simuladas
echo
echo "--- 4. Testes de Contas e Isolamento OAuth ---"

if [[ -f "$RAIZ_CATAMARA/core/contas/contas.py" ]]; then
  # Inicializacao
  python3 "$RAIZ_CATAMARA/core/contas/contas.py" init >/dev/null 2>&1
  if [[ -d "$CATAMARA_CONTAS_DIR/conta1/antigravity-cli" && -d "$CATAMARA_CONTAS_DIR/conta2/antigravity-cli" ]]; then
    registrar_resultado "contas.py init cria arvores isoladas" OK
  else
    registrar_resultado "contas.py init cria arvores isoladas" FALHA "diretorios de conta nao foram criados"
  fi

  # Injeta credenciais simuladas com JWT falso
  payload_c1="$(printf '{"email":"c1@exemplo.test"}' | base64 | tr '/+' '_-' | tr -d '=')"
  payload_c2="$(printf '{"email":"c2@exemplo.test"}' | base64 | tr '/+' '_-' | tr -d '=')"
  jwt_c1="eyJhbGciOiJub25lIn0.${payload_c1}."
  jwt_c2="eyJhbGciOiJub25lIn0.${payload_c2}."

  campo_ref="refresh""_token"
  cat <<EOF > "$CATAMARA_CONTAS_DIR/conta1/antigravity-cli/antigravity-oauth-token"
{"auth_method":"oauth","id_token":"$jwt_c1","token":{"access_token":"segredo_acc_1","$campo_ref":"segredo_ref_1"}}
EOF
  cat <<EOF > "$CATAMARA_CONTAS_DIR/conta2/antigravity-cli/antigravity-oauth-token"
{"auth_method":"oauth","id_token":"$jwt_c2","token":{"access_token":"segredo_acc_2","$campo_ref":"segredo_ref_2"}}
EOF
  chmod 600 "$CATAMARA_CONTAS_DIR/conta1/antigravity-cli/antigravity-oauth-token"
  chmod 600 "$CATAMARA_CONTAS_DIR/conta2/antigravity-cli/antigravity-oauth-token"

  mkdir -p "$CATAMARA_ESTADO/cotas"
  cat <<'EOF' > "$CATAMARA_ESTADO/cotas/conta1.json"
{"conta":"conta1","ok":true,"atualizado_em_epoch":9999999999,"fonte":"cache","piscinas":{"gemini-argon-wtu-weekly":{"restante_pct":80.0},"gemini-5h":{"restante_pct":75.0},"gemini-weekly":{"restante_pct":70.0},"3p-5h":{"restante_pct":60.0},"3p-weekly":{"restante_pct":50.0}}}
EOF
  cat <<'EOF' > "$CATAMARA_ESTADO/cotas/conta2.json"
{"conta":"conta2","ok":true,"atualizado_em_epoch":9999999999,"fonte":"cache","piscinas":{"gemini-argon-wtu-weekly":{"restante_pct":90.0},"gemini-5h":{"restante_pct":85.0},"gemini-weekly":{"restante_pct":80.0},"3p-5h":{"restante_pct":95.0},"3p-weekly":{"restante_pct":90.0}}}
EOF

  status_out="$(python3 "$RAIZ_CATAMARA/core/contas/contas.py" status 2>/dev/null || echo "")"
  if echo "$status_out" | grep -q "c1@exemplo.test" && echo "$status_out" | grep -q "c2@exemplo.test"; then
    if echo "$status_out" | grep -q "segredo_acc_"; then
      registrar_resultado "contas.py status nao vaza tokens sensiveis" FALHA "token sensivel encontrado na saida"
    else
      registrar_resultado "contas.py status reconhece e-mails e protege segredos" OK
    fi
  else
    registrar_resultado "contas.py status reconhece e-mails e protege segredos" FALHA "e-mails simulados nao reconhecidos"
  fi

  # Escolha de conta oposta
  esc_rev_1="$(python3 "$RAIZ_CATAMARA/core/contas/contas.py" escolher --papel revisor --evitar conta1 --apenas-nome 2>/dev/null || echo "")"
  esc_rev_2="$(python3 "$RAIZ_CATAMARA/core/contas/contas.py" escolher --papel revisor --evitar conta2 --apenas-nome 2>/dev/null || echo "")"
  if [[ "$esc_rev_1" == "conta2" && "$esc_rev_2" == "conta1" ]]; then
    registrar_resultado "contas.py escolher evita conta especificada" OK
  else
    registrar_resultado "contas.py escolher evita conta especificada" FALHA "retornou esc1=$esc_rev_1 esc2=$esc_rev_2"
  fi

  # Sincronizacao de confianca
  python3 "$RAIZ_CATAMARA/core/contas/contas.py" confianca adicionar "$TMP/repo" >/dev/null 2>&1
  s1="$CATAMARA_CONTAS_DIR/conta1/antigravity-cli/settings.json"
  s2="$CATAMARA_CONTAS_DIR/conta2/antigravity-cli/settings.json"
  if grep -q "$TMP/repo" "$s1" 2>/dev/null && grep -q "$TMP/repo" "$s2" 2>/dev/null; then
    registrar_resultado "contas.py confianca adicionar sincroniza caminhos" OK
  else
    registrar_resultado "contas.py confianca adicionar sincroniza caminhos" FALHA "caminho nao adicionado aos settings.json"
  fi
else
  registrar_resultado "modulo de contas (contas.py)" PULADO "arquivo ausente"
fi

# 5. Testes da CLI catamara e cata
echo
echo "--- 5. Testes da Interface CLI (catamara e cata) ---"

if [[ -x "$RAIZ_CATAMARA/bin/catamara" ]]; then
  if "$RAIZ_CATAMARA/bin/catamara" -h >/dev/null 2>&1; then
    registrar_resultado "catamara -h retorna 0" OK
  else
    registrar_resultado "catamara -h retorna 0" FALHA "codigo de saida nao foi 0"
  fi

  if [[ -L "$RAIZ_CATAMARA/bin/cata" ]] && "$RAIZ_CATAMARA/bin/cata" -h >/dev/null 2>&1; then
    registrar_resultado "symlink cata -h retorna 0" OK
  else
    registrar_resultado "symlink cata -h retorna 0" FALHA "falha ao executar cata"
  fi

  if "$RAIZ_CATAMARA/bin/catamara" config --json 2>/dev/null | jq -e '.CATAMARA_PATH' >/dev/null 2>&1; then
    registrar_resultado "catamara config --json emite json valido" OK
  else
    registrar_resultado "catamara config --json emite json valido" FALHA "json invalido ou incompleto"
  fi

  sub_invalido_status=0
  "$RAIZ_CATAMARA/bin/catamara" subcomando_inexistente >/dev/null 2>&1 || sub_invalido_status=$?
  if [[ $sub_invalido_status -eq 2 ]]; then
    registrar_resultado "catamara com subcomando invalido sai com 2" OK
  else
    registrar_resultado "catamara com subcomando invalido sai com 2" FALHA "codigo de saida foi $sub_invalido_status"
  fi
else
  registrar_resultado "bin/catamara" PULADO "binario nao executavel ou ausente"
fi

# 6. Testes do Verificador Deterministico (Fase 3)
echo
echo "--- 6. Testes do Verificador Deterministico ---"

DET_PY="$RAIZ_CATAMARA/core/orquestracao/deterministico.py"
if [[ -x "$DET_PY" ]]; then
  REPO_TESTE="$TMP/repo_teste"
  mkdir -p "$REPO_TESTE"
  git -C "$REPO_TESTE" init -q
  git -C "$REPO_TESTE" config user.email "test@exemplo.test"
  git -C "$REPO_TESTE" config user.name "Tester"
  echo "echo ok" > "$REPO_TESTE/valido.sh"
  chmod +x "$REPO_TESTE/valido.sh"
  git -C "$REPO_TESTE" add .
  git -C "$REPO_TESTE" commit -q -m "inicial"

  # Teste em repositorio limpo
  if python3 "$DET_PY" --repo "$REPO_TESTE" --json >/dev/null 2>&1; then
    registrar_resultado "deterministico.py em repositorio limpo retorna 0" OK
  else
    registrar_resultado "deterministico.py em repositorio limpo retorna 0" FALHA "falhou em repo limpo"
  fi

  # Teste de marcador de conflito (gerado dinamicamente para evitar falso positivo)
  ARQ_CONF="$REPO_TESTE/conflito.txt"
  {
    printf '<%.0s' {1..7}
    printf ' HEAD\nlinha A\n'
    printf '=%.0s' {1..7}
    printf '\nlinha B\n'
    printf '>%.0s' {1..7}
    printf ' branch\n'
  } > "$ARQ_CONF"
  conf_status=0
  python3 "$DET_PY" --repo "$REPO_TESTE" --json >/dev/null 2>&1 || conf_status=$?
  if [[ $conf_status -eq 1 ]]; then
    registrar_resultado "deterministico.py detecta marcadores de conflito git" OK
  else
    registrar_resultado "deterministico.py detecta marcadores de conflito git" FALHA "codigo foi $conf_status"
  fi
  rm -f "$ARQ_CONF"

  # Teste de script shell quebrado
  echo 'if [[ a == b ]; then' > "$REPO_TESTE/quebrado.sh"
  sh_status=0
  python3 "$DET_PY" --repo "$REPO_TESTE" --json >/dev/null 2>&1 || sh_status=$?
  if [[ $sh_status -eq 1 ]]; then
    registrar_resultado "deterministico.py detecta erro de sintaxe bash" OK
  else
    registrar_resultado "deterministico.py detecta erro de sintaxe bash" FALHA "codigo foi $sh_status"
  fi
  rm -f "$REPO_TESTE/quebrado.sh"

  # Teste de script Python quebrado
  echo 'def func(;' > "$REPO_TESTE/quebrado.py"
  py_status=0
  python3 "$DET_PY" --repo "$REPO_TESTE" --json >/dev/null 2>&1 || py_status=$?
  if [[ $py_status -eq 1 ]]; then
    registrar_resultado "deterministico.py detecta erro de sintaxe python" OK
  else
    registrar_resultado "deterministico.py detecta erro de sintaxe python" FALHA "codigo foi $py_status"
  fi
  rm -f "$REPO_TESTE/quebrado.py"

  # Teste de deteccao de segredo falso (gerado em runtime)
  SEGREDO_FALSO="$(printf '%s' "ya""29.simulado_token_oauth_teste_1234567890")"
  echo "TOKEN = '$SEGREDO_FALSO'" > "$REPO_TESTE/segredo.py"
  seg_status=0
  python3 "$DET_PY" --repo "$REPO_TESTE" --json >/dev/null 2>&1 || seg_status=$?
  if [[ $seg_status -eq 1 ]]; then
    registrar_resultado "deterministico.py detecta segredo simulado" OK
  else
    registrar_resultado "deterministico.py detecta segredo simulado" FALHA "codigo foi $seg_status"
  fi
  rm -f "$REPO_TESTE/segredo.py"

  # Teste de comando opcional --teste
  tst_status=0
  python3 "$DET_PY" --repo "$REPO_TESTE" --teste "false" --json >/dev/null 2>&1 || tst_status=$?
  if [[ $tst_status -eq 1 ]]; then
    registrar_resultado "deterministico.py falha quando --teste retorna nao-zero" OK
  else
    registrar_resultado "deterministico.py falha quando --teste retorna nao-zero" FALHA "codigo foi $tst_status"
  fi
else
  registrar_resultado "deterministico.py" PULADO "arquivo ausente"
fi

# 7. Testes do Validador (catamara-validar)
echo
echo "--- 7. Testes do Validador (catamara-validar) ---"

VALIDAR_BIN="$RAIZ_CATAMARA/bin/catamara-validar"
if [[ -x "$VALIDAR_BIN" ]]; then
  if "$VALIDAR_BIN" --so-deterministico --repo "$RAIZ_CATAMARA" --json >/dev/null 2>&1; then
    registrar_resultado "catamara-validar --so-deterministico conclui com sucesso" OK
  else
    registrar_resultado "catamara-validar --so-deterministico conclui com sucesso" FALHA "falhou no proprio repositorio"
  fi

  if [[ -f "$CATAMARA_ESTADO/validar.jsonl" ]] && grep -q '"deterministico_ok":true' "$CATAMARA_ESTADO/validar.jsonl"; then
    registrar_resultado "catamara-validar registra recibo em validar.jsonl" OK
  else
    registrar_resultado "catamara-validar registra recibo em validar.jsonl" FALHA "recibo ausente em validar.jsonl"
  fi
else
  registrar_resultado "catamara-validar" PULADO "arquivo ausente"
fi

# 8. Testes do Gerenciador de Sessoes (sessoes.py)
echo
echo "--- 8. Testes do Gerenciador de Sessoes (sessoes.py) ---"

SESSOES_PY="$RAIZ_CATAMARA/core/orquestracao/sessoes.py"
if [[ -x "$SESSOES_PY" ]]; then
  saida_sessoes="$(python3 "$SESSOES_PY" listar --json 2>/dev/null || echo "")"
  if echo "$saida_sessoes" | jq -e '.sessoes | type == "array"' >/dev/null 2>&1; then
    registrar_resultado "sessoes.py listar --json devolve lista valida" OK
  else
    registrar_resultado "sessoes.py listar --json devolve lista valida" FALHA "saida json invalida"
  fi
else
  registrar_resultado "sessoes.py" PULADO "arquivo ausente"
fi

# 9. Testes de Delegacao e Extracao (Fase 2, se disponiveis)
echo
echo "--- 9. Testes da Fase 2 (Extracao, Implementacao, Hook) ---"

EXTRACAO_PY="$RAIZ_CATAMARA/core/delegacao/extracao.py"
if [[ -f "$EXTRACAO_PY" ]]; then
  if python3 "$EXTRACAO_PY" validar-esquema "id:int" "nome:texto" >/dev/null 2>&1; then
    registrar_resultado "extracao.py validar-esquema aceita esquema valido" OK
  else
    registrar_resultado "extracao.py validar-esquema aceita esquema valido" FALHA "rejeitou esquema valido"
  fi

  inv_esq=0
  python3 "$EXTRACAO_PY" validar-esquema "id:tipo_invalido" >/dev/null 2>&1 || inv_esq=$?
  if [[ $inv_esq -ne 0 ]]; then
    registrar_resultado "extracao.py validar-esquema rejeita esquema invalido" OK
  else
    registrar_resultado "extracao.py validar-esquema rejeita esquema invalido" FALHA "aceitou tipo desconhecido"
  fi

  if python3 "$EXTRACAO_PY" schema-json "valor:numero" 2>/dev/null | jq -e '.properties.registros' >/dev/null 2>&1; then
    registrar_resultado "extracao.py schema-json devolve json schema compativel" OK
  else
    registrar_resultado "extracao.py schema-json devolve json schema compativel" FALHA "schema-json invalido"
  fi
else
  registrar_resultado "core/delegacao/extracao.py" PULADO "arquivo da Fase 2 ainda nao presente"
fi

HOOK_BIN="$RAIZ_CATAMARA/bin/catamara-hook-leitor"
if [[ -x "$HOOK_BIN" ]]; then
  registrar_resultado "bin/catamara-hook-leitor presente e executavel" OK
else
  registrar_resultado "bin/catamara-hook-leitor" PULADO "arquivo da Fase 2 ainda nao presente"
fi

DELEGAR_BIN="$RAIZ_CATAMARA/bin/catamara-delegar"
if [[ -x "$DELEGAR_BIN" ]]; then
  if "$DELEGAR_BIN" --ajuda >/dev/null 2>&1; then
    registrar_resultado "catamara-delegar --ajuda sai com 0" OK
  else
    registrar_resultado "catamara-delegar --ajuda sai com 0" FALHA "falha ao exibir ajuda"
  fi
else
  registrar_resultado "bin/catamara-delegar" PULADO "arquivo da Fase 2 ainda nao presente"
fi

# 10. Testes da Central de Tarefas e Painel Operacional
echo
echo "--- 10. Testes da Central de Tarefas e Painel ---"

SERVIDOR_PY="$RAIZ_CATAMARA/core/painel/servidor.py"
if [[ -f "$SERVIDOR_PY" ]]; then
  if python3 "$SERVIDOR_PY" --snapshot-json 2>/dev/null | jq -e '.indicadores and .resumo_barra' >/dev/null 2>&1; then
    registrar_resultado "servidor.py --snapshot-json emite json valido com indicadores" OK
  else
    registrar_resultado "servidor.py --snapshot-json emite json valido com indicadores" FALHA "saida json invalida"
  fi
else
  registrar_resultado "core/painel/servidor.py" PULADO "arquivo ausente"
fi

CATAMARA_BIN="$RAIZ_CATAMARA/bin/catamara"
if [[ -x "$CATAMARA_BIN" ]]; then
  if "$CATAMARA_BIN" tarefas --status >/dev/null 2>&1; then
    registrar_resultado "catamara tarefas --status sai com 0" OK
  else
    registrar_resultado "catamara tarefas --status sai com 0" FALHA "comando tarefas falhou"
  fi

  if "$CATAMARA_BIN" tarefas --waybar 2>/dev/null | jq -e '.text and .tooltip' >/dev/null 2>&1; then
    registrar_resultado "catamara tarefas --waybar emite json compativel com waybar" OK
  else
    registrar_resultado "catamara tarefas --waybar emite json compativel com waybar" FALHA "saida waybar invalida"
  fi
else
  registrar_resultado "bin/catamara" PULADO "arquivo ausente"
fi

# Resumo Final
echo
echo "=========================================="
printf 'Total: %d | OK: %d | Falhas: %d | Pulados: %d\n' "$TOTAL_TESTES" "$TESTES_OK" "$TESTES_FALHA" "$TESTES_PULADOS"
echo "=========================================="

if [[ $TESTES_FALHA -eq 0 ]]; then
  echo "Sucesso: Todos os testes executados passaram sem falhas."
  exit 0
else
  echo "Erro: Foram detectadas $TESTES_FALHA falha(s)."
  exit 1
fi
