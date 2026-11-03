# NotebookAI — Arquitetura (pré-V0.1)

Prioridade: precisão dos dados > robustez > baixo consumo > manutenção > estética.

## 1. Arquitetura completa

Dois processos independentes, comunicando-se apenas pelo SQLite (modo WAL) e por um canal local leve.

```
HWiNFO (Shared Memory) ─┐
psutil / Win32 / WMI ───┤
PresentMon (CSV stream) ─┼─► Agent: Coletores ─► Normalizador ─► Motor de Intervalos ─► SQLite (WAL)
Event Log (pywin32) ────┤                              │                │
Power/Session msgs ─────┘                              └─► Detector de eventos críticos ─► tabela events
                                                                         │
                                                       Escritor TXT diário ◄┘
GUI (PySide6) ◄── leitura SQLite (somente leitura) + pipe nomeado para "ao vivo"
```

Camadas do Agent:
- **Coletores**: cada fonte implementa `Collector.sample() -> dict[str, Reading]`. Falha de uma fonte não derruba as outras.
- **Normalizador**: nomes de sensores viram chaves canônicas (`cpu.package_temp_c`, `gpu.util_pct`, `ram.used_gb`), com unidade e fonte.
- **Motor de intervalos**: decide continuar, fechar ou abrir intervalo (item 7).
- **Detector de eventos**: regras de severidade crítica (item 8).
- **Persistência**: fila em memória, gravação em lote (flush a cada 30 s ou em evento crítico).

## 2. Estrutura de pastas

Código-fonte:
```
notebookai/
├── agent/
│   ├── main.py            # loop principal, agendador
│   ├── collectors/        # hwinfo.py, system.py, processes.py, foreground.py, battery.py, presentmon.py, eventlog.py
│   ├── normalize.py
│   ├── intervals.py
│   ├── events.py
│   ├── sessions.py
│   ├── store.py           # SQLite
│   ├── txtlog.py
│   └── lifecycle.py       # shutdown, sleep, flush
├── gui/                   # PySide6: janelas, páginas, tema
├── common/                # config, schema, constantes
├── tests/                 # simulador e testes
└── packaging/             # PyInstaller specs, instalador
```
Instalação em tempo de execução:
```
NotebookAI/
├── NotebookAI.exe         # GUI + bandeja
├── NotebookAIAgent.exe
├── Data/notebookai.db
├── Logs/AAAA-MM-DD.txt
├── Exports/
└── Config/config.toml
```

## 3. Modelo do banco SQLite

Princípio: guardar **intervalos**, não amostras.

```sql
sensors(id, key UNIQUE, label, unit, source, first_seen)          -- catálogo dinâmico
intervals(id, start_ts, end_ts, state, fg_app_id, close_reason)
interval_stats(interval_id, sensor_id, min, max, avg, n)          -- 1 linha por sensor por intervalo
apps(id, exe_path UNIQUE, exe_name, friendly_name)
app_sessions(id, app_id, pid, start_ts, end_ts, foreground_secs,
             cpu_avg, cpu_max, gpu_avg, gpu_max, ram_avg_mb, ram_max_mb,
             temp_cpu_max, power_avg_w, fps_avg, fps_1low, fps_01low, load_class)
events(id, ts, type, severity, detail_json, interval_id)
battery_snapshots(ts, design_mwh, full_mwh, remaining_mwh, voltage_mv, rate_mw,
                  status, ac, cycles, health_pct)                 -- 1 por hora ou por mudança
battery_capacity_history(day, full_mwh_min, full_mwh_max, design_mwh)
power_states(id, start_ts, end_ts, ac, profile)
system_boots(id, boot_ts, shutdown_ts, shutdown_reason, clean)
meta(key, value)                                                  -- versão do schema
```
Índices: `intervals(start_ts)`, `interval_stats(sensor_id, interval_id)`, `events(ts,type)`, `app_sessions(app_id,start_ts)`.
Retenção: intervalos e eventos permanentes (poucos MB por mês); nada em resolução de segundo.

## 4. Comunicação com HWiNFO

- HWiNFO precisa rodar com **Shared Memory Support** ativado (Settings > General; limite de 12 h na versão gratuita, o que exige reativação ou licença Pro; o agente detecta e avisa).
- Leitura via `mmap` de `Global\HWiNFO_SENS_SM2`, parseando `HWiNFO_SENSORS_SHARED_MEM2` (header, array de sensores, array de readings com `szLabelOrig`, `szUnit`, `Value`, `ValueMin/Max/Avg`).
- Descoberta dinâmica: a cada início e a cada 60 s revalida o conjunto; chaves canônicas via tabela de regras por regex (`Package|CPU Package`, `Core \d+ T`, `Total GPU Power`...). Sensores sem regra entram como `hwinfo.<sensor>.<label>` e são armazenados mesmo assim.
- Se a memória compartilhada não existir, fallback parcial (psutil/WMI) e evento `hwinfo_unavailable`.

## 5. Identificação de aplicativos

- Primeiro plano: `GetForegroundWindow` → `GetWindowThreadProcessId` → `psutil.Process`.
- Nome amigável: `FileDescription` do VERSIONINFO do exe, com regras (ex.: `javaw.exe` + título da janela contendo "Minecraft" → "Minecraft Java").
- Carga por processo: amostragem de `cpu_percent`, `memory_info`, I/O; GPU por processo via contadores PDH `GPU Engine(pid_*)`.
- **Aberto vs causando carga**: um processo é "ativo" quando CPU > 5% por 30 s, ou GPU > 10%, ou está em primeiro plano com input recente (`GetLastInputInfo`). Caso contrário permanece "aberto". Cada sessão guarda `foreground_secs` e `load_class`.
- Agrupamento: Chrome e similares somam todos os processos filhos por exe.

## 6. Captura de FPS

- **PresentMon** (console app, distribuído separadamente por licença MIT) lançado pelo agente com `--output_stdout` (CSV em fluxo), filtrado ao PID em primeiro plano.
- Calcula média, 1% low, 0,1% low e frametime por janela de 5 s; só persiste quando o app está ativo e é gráfico.
- Requer privilégio de administrador (ETW); o agente roda como tarefa agendada elevada.
- Fica na V0.3; na V0.1 só a interface do coletor existe.

## 7. Algoritmo de intervalos inteligentes

Amostragem interna a 1–2 s, persistência somente nas mudanças.

Para cada sensor há um perfil configurável: `tolerance_small`, `tolerance_significant`, `critical`. Exemplo (config.toml):
```
[ram.used_gb]  small = 0.5  significant = 1.5
[cpu.util_pct] small = 15   significant = 30
[cpu.package_temp_c] small = 5  significant = 10  critical = 95
```
Regras por amostra:
1. Mantém estatísticas do intervalo corrente (min, max, soma, n) por sensor.
2. Mudança pequena: |valor − média_móvel| ≤ small e a faixa do intervalo ≤ significant → continua.
3. Mudança significativa: a faixa (max−min) excede `significant` **ou** o valor sai da banda por mais de `confirm_secs` (padrão 10 s, para filtrar picos) → fecha o intervalo e abre outro.
4. Mudança de estado discreto (app em primeiro plano, AC, perfil de energia) → fecha sempre, com debounce de 3 s.
5. Crítico → fecha imediatamente, grava evento.
6. Teto de duração (padrão 6 h) e rotação à meia-noite, para que o TXT diário feche coerente.
Votação de sensores: apenas sensores "guia" (CPU, GPU, RAM, temp, potência, app em primeiro plano, AC) decidem o corte; os demais acompanham o intervalo e só registram min/max/avg.

## 8. Sistema de eventos críticos

Regras declarativas com histerese (para não gerar eventos repetidos):
- Thermal throttling, Power limit, Current/EDP limit (flags do HWiNFO), temperatura ≥ limite configurável.
- RAM > 90%, commit > 90% do limite.
- Queda brusca de FPS (> 30% em 5 s) e frametime anormal (> 3× a mediana).
- WHEA, BSOD (Kernel-Power 41, BugCheck 1001), erros de disco/hardware: leitura do Event Log com subscription (`EvtSubscribe`).
- Bateria ≤ 5%, AC conectado/desconectado, mudança de estado de energia, sleep/wake.
- Processo com carga excepcional (> 3× a própria mediana de 7 dias, ou > 90% de CPU por 60 s).
Cada evento: `type`, `severity`, `detail_json` (valores no momento, app em primeiro plano, últimos estados) e fecha o intervalo.

## 9. Estratégia de bateria

- Fontes: `IOCTL_BATTERY_QUERY_INFORMATION` (Windows Battery API: `BatteryInformation` com `DesignedCapacity`, `FullChargedCapacity`, `CycleCount`; `BatteryStatus` com `Capacity`, `Voltage`, `Rate`) e sensores do HWiNFO como validação cruzada.
- Nada de constante fixa: a capacidade de projeto e a plena são lidas a cada coleta; os 58.003 mWh só entram como valor de conferência no primeiro registro.
- Saúde = `FullChargedCapacity / DesignedCapacity`; desgaste = 1 − saúde.
- Histórico: `battery_snapshots` a cada mudança de `FullChargedCapacity` e no mínimo 1 por dia; `battery_capacity_history` para tendência (taxa de perda por mês).
- mAh só é exibido quando a tensão nominal do pacote é confiável (tensão nominal da bateria via `BatteryInformation`/HWiNFO); caso contrário fica "n/d".
- Para a IA: o TXT inclui perfil de uso (percentual do tempo na tomada, horas fora da tomada, descarga média em W, autonomia estimada com a capacidade atual) e a tendência de queda, não só o percentual de saúde.
- Em 5%: flush imediato e evento; **nenhuma ação de desligar**.

## 10. Arquitetura GUI + Agent

- Agent: serviço em segundo plano (processo próprio, sem janela), dono exclusivo da escrita no banco.
- GUI: PySide6, abre em leitura (SQLite `mode=ro`, WAL permite leitura concorrente).
- Dados ao vivo: pipe nomeado local `\\.\pipe\NotebookAI` com um instantâneo JSON a 1 Hz, **só enquanto houver GUI conectada** (custo zero com a janela fechada).
- Bandeja: ícone controlado pela GUI, mas o Agent continua se a GUI fechar.
- Duas visões: simples (dashboard) e detalhada (todos os sensores com fonte indicada).

## 11. Estratégia de autostart

- Tarefa agendada do Windows (`schtasks`, gatilho "ao fazer logon", execução com privilégios mais altos) para o Agent; permite o acesso exigido pelo PresentMon sem prompt de UAC a cada login.
- GUI inicia minimizada na bandeja via chave `HKCU\...\Run` (opcional).
- O agente verifica a tarefa na inicialização e a recria se faltar.

## 12. Estratégia de shutdown

- Janela oculta recebendo `WM_QUERYENDSESSION`/`WM_ENDSESSION` e `WM_POWERBROADCAST` (suspend/resume); `SetConsoleCtrlHandler` para os demais casos.
- Sequência: fechar intervalo com `close_reason` (shutdown, restart, logoff, sleep, hibernate), gravar `system_boots.shutdown_*`, checkpoint do WAL, anexar ao TXT, `fsync`, sair.
- Distinção shutdown/restart: leitura de `ShutdownReason`/eventos 1074 do Event Log na próxima inicialização.
- Desligamento sujo (queda de energia/BSOD): na inicialização o agente detecta intervalo aberto sem fechamento, fecha-o no último timestamp conhecido (heartbeat gravado a cada 30 s) e registra `unclean_shutdown`.

## 13. Estratégia de exportação TXT

- TXT diário gerado incrementalmente a partir de intervalos fechados e eventos; formato estável, determinístico e compacto, com cabeçalho do dia (hardware, resumo, bateria).
- Exportação consolidada: percorre os dias em intervalo, concatena com separadores `====` e um **resumo estatístico no topo** (horas por app, picos, eventos por tipo, tendência da bateria) para a IA não depender de ler tudo.
- Estimativa de tamanho: ~20–60 KB por dia típico; um mês cabe no contexto de uma IA moderna, com opção de nível de detalhe ("resumo", "padrão", "completo").

## 14. Estimativa de consumo

Estimativas de projeto (a validar na V1.0 com medição real):
- CPU em idle: 0,3–0,8% (amostragem a 1–2 s, processos a 5 s, uma varredura da lista completa a cada 15 s).
- RAM do Agent: 45–90 MB em Python empacotado (sem Qt); a GUI separada usa 120–200 MB só enquanto aberta.
- Disco: gravações em lote a cada 30 s (WAL), algo como 1–3 MB/dia no banco.
- Decisões que mantêm o custo baixo: nada de Qt no agente, `psutil.Process.oneshot()`, sem varredura por processo mais frequente que 5 s, PresentMon só com app gráfico ativo. Se a meta de 150 MB ficar apertada, a alternativa é reescrever os coletores em Rust/C#, que eu apresentaria antes de trocar.

## 15. Como a interface de referência será implementada

- PySide6 com QSS (tema único) e `QGraphicsBlurEffect` apenas nos fundos estáticos; o "vidro" é translucidez sobre um gradiente pré-renderizado (escuro `#050B14` → `#0A1A26`), bordas de 1 px a `rgba(202,233,255,0.10)`, cantos de 14–16 px, sombras suaves.
- Texto `#CAE9FF` com variações de opacidade para hierarquia; acentos teal `#2DD4BF`, ciano `#38BDF8`, azul `#3B82F6`. Fonte Inter embutida (licença OFL) com números tabulares.
- Layout da referência: barra lateral fixa (Painel Principal, Histórico, Aplicativos, CPU, GPU, RAM, SSD, Bateria, Eventos, Configurações, Sobre), cabeçalho "Monitorando..." com 4 métricas, grade de cartões com sparklines, cartão do app em primeiro plano, gráfico "Uso ao Longo do Tempo" e lista de eventos recentes.
- Gráficos: `QPainter` próprio (sparklines leves) e `pyqtgraph` ou desenho próprio para o gráfico principal; sem animação contínua, para economizar GPU.
- Páginas detalhadas usam tabelas limpas (não "planilha"): grupos com rótulo, valor, min/max e a **fonte** de cada sensor.
- Bateria: barra de saúde e a escada de capacidade (58.003 → ... → atual) a partir do histórico.
- Referência usada como linguagem visual, não copiada pixel a pixel; sem emoji e sem neon.
