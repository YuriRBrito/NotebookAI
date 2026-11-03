# NotebookAI Agent — V0.1.1 (coletores reais + diagnóstico)

Estado honesto: **todo o código dos coletores do Windows foi escrito, mas nunca rodou em um Windows**. Foi compilado e a lógica pura foi testada no Linux (parser do HWiNFO com buffer sintético, cálculo de bateria, classificação aberto×ativo, agregação de GPU, parser do Event Log, isolamento de falhas, motor de intervalos, `--monitor` com frames simulados). Os offsets das estruturas do HWiNFO, os nomes dos contadores PDH e os IOCTLs da bateria seguem a documentação pública como eu a conheço, e **o primeiro `--diagnostic` no seu Latitude é o teste de verdade**. Se algo falhar, o relatório diz qual coletor e por quê.

## 1. Pré-requisitos (Windows 11)

1. Python 3.10 ou superior (python.org; marque "Add to PATH").
2. Dependência: `pip install psutil`  (só ela; o resto usa ctypes, PowerShell e `wevtutil`, que já vêm no Windows).
3. HWiNFO (versão "Sensors-only" ou completa), configurado como na seção 2.
4. Copie a pasta `notebookai` para o notebook e abra um terminal comum (PowerShell) dentro dela.

## 2. Configurar o HWiNFO

1. Abra o HWiNFO e escolha **Sensors-only** (ou abra a janela de sensores).
2. Engrenagem (Settings) → aba **General / User Interface** → marque **Shared Memory Support**.
3. Deixe a janela de sensores aberta/minimizada (a memória compartilhada só existe enquanto o HWiNFO roda com os sensores ativos).
4. Aviso: na versão gratuita o Shared Memory costuma ter limite de tempo por sessão (eu lembro de 12 h; **confira no seu HWiNFO**). Para monitoramento contínuo pode ser necessário licença Pro.
5. Se o HWiNFO estiver rodando como administrador e o Agent não, o Windows pode negar a leitura (erro 5). O diagnóstico mostra isso explicitamente; a correção é rodar os dois no mesmo nível, e é um dos testes da seção 5.

## 3. Comandos

Rode sempre na pasta do projeto.

```
python NotebookAIAgent.py --diagnostic
python NotebookAIAgent.py --list-sensors
python NotebookAIAgent.py --list-sensors --filter battery
python NotebookAIAgent.py --monitor --minutes 5
python NotebookAIAgent.py --monitor --minutes 0          (até Ctrl+C)
python NotebookAIAgent.py --apps --seconds 120
python NotebookAIAgent.py --apps --seconds 120 --watch minecraft
python ShowDb.py                                        (mostra o que foi gravado)
python tests/test_logic.py                              (testes de lógica)
```

Opções úteis: `--root PASTA` (onde ficam Data/Logs/Exports; padrão `NotebookAI_runtime`), `--nominal-voltage-mv 11400` (habilita "Estimated mAh", veja seção 6), `--reference-design-mwh 58003` (valor de validação).

O `.exe` (`NotebookAIAgent.exe --diagnostic`) vem depois, com PyInstaller; nesta etapa você roda o `.py`.

## 4. Roteiro de validação no Latitude 5430

1. `--diagnostic` normal (sem administrador). Guarde o arquivo em `Exports/`.
2. `--diagnostic` como administrador. Compare os dois relatórios (o nome do arquivo termina em `_user` ou `_admin`).
3. `--list-sensors`: confira se CPU, GPU, bateria, SSD e RAM aparecem e quais ficaram sem `*` (não mapeados).
4. `--apps --watch minecraft` com o jogo (a) fechado, (b) aberto e minimizado, (c) em primeiro plano jogando. Esperado: (b) `OPEN / LOW`, (c) `ACTIVE / HIGH`.
5. `--apps` alternando Chrome, Edge, Explorer, terminal, VS Code: o primeiro plano não depende do Minecraft.
6. `--monitor --minutes 10`: deixe parado 3 min, abra um app pesado, conecte/desconecte o carregador. Depois `python ShowDb.py` e abra `NotebookAI_runtime/Logs/AAAA-MM-DD.txt`.
7. Teste de falhas: feche o HWiNFO durante o `--monitor`. Deve aparecer `## HWiNFO: OK -> UNAVAILABLE` e o resto continuar. Reabra o HWiNFO e deve voltar.

## 5. Precisa de administrador? (o que eu sei e o que não sei)

Não testei nada disso. A coluna "esperado" é minha expectativa; a verdade sai do passo 2 acima.

| Coletor | Esperado | Observação |
|---|---|---|
| HWiNFO (leitura) | não | Pode dar erro 5 se o HWiNFO estiver elevado e o Agent não |
| psutil / processos | não | Processos protegidos ficam inacessíveis e são contados em "inacessíveis" |
| Primeiro plano | não | Janelas de apps elevados continuam legíveis; o caminho pode vir por `QueryFullProcessImageName` |
| Battery API (IOCTL) | não | Abre o dispositivo de bateria com leitura/escrita; se negar, aparece UNAVAILABLE com o erro |
| Event Log (System) | não | O log Security exigiria admin e **não é usado** |
| SSD: SMART/vida útil | talvez | Via HWiNFO se o sensor existir; Get-PhysicalDisk só dá o resumo de saúde |
| PresentMon (FPS, V0.3) | sim, ou grupo "Performance Log Users" | ETW. Provavelmente dá para evitar admin pleno adicionando seu usuário a esse grupo; a confirmar |

Conclusão de projeto: o Agent inteiro **não** deve rodar elevado; se o PresentMon exigir, ele vira um processo auxiliar isolado.

## 6. O que o NotebookAI sabe e o que não sabe (regra 21)

| Item | O que tentamos | Fonte | Situação |
|---|---|---|---|
| Design/Full/Remaining da bateria | ler do firmware | Windows Battery IOCTL (`BatteryInformation`/`BatteryStatus`) | Medição real. 58.003 mWh só é usado para comparar |
| Health e Wear | Full ÷ Design | cálculo sobre o IOCTL | Calculado, rotulado `calculated` |
| Corrente da bateria | amperímetro | não existe na API | **Calculada** como Rate(mW) ÷ Voltage(mV), instantânea |
| Ciclos | `CycleCount` | IOCTL | Muitos firmwares devolvem 0 = não informa; o relatório avisa |
| **mAh** | capacidade em mAh | a API entrega mWh | **N/D**, pois exige a tensão *nominal* do pacote, que a API não dá e a tensão instantânea não serve. Com `--nominal-voltage-mv` sai "Estimated mAh" (estimativa) |
| Tempo restante / até carregar | estimativa do Windows | IOCTL e `GetSystemPowerStatus` | Mostra as duas fontes e uma **linear** (rotulada estimativa); a carga real desacelera perto de 100% |
| Clock atual da CPU | frequência instantânea | HWiNFO (média dos clocks por núcleo, `calculated`) | `psutil.cpu_freq` no Windows costuma ser nominal; o PDH é derivado. Mostro as três separadas, sem escolher em silêncio |
| Effective Clock, Core Power | HWiNFO | depende dos rótulos do seu HWiNFO | A confirmar no `--list-sensors` |
| PL1 / PL2 | HWiNFO | rótulos variam; o Windows não expõe | Provável **N/D**; se o HWiNFO não tiver, só leitura de MSR/driver resolveria, e eu não faria isso sem avisar |
| Thermal/Power/EDP limit | flags Yes/No do HWiNFO | HWiNFO | Se vários sensores, é o máximo deles (YES se qualquer um) |
| Temperatura da GPU Iris Xe | HWiNFO | HWiNFO | GPU integrada costuma **não ter sensor próprio**; esperado N/D (a temperatura do package da CPU não é "substituída" em silêncio) |
| Potência da GPU | HWiNFO | HWiNFO | Pode existir como "IA GPU Power"/similar; a confirmar |
| Uso da GPU | contadores Windows | PDH `GPU Engine` (maior soma por engine) | Medição derivada; também por processo |
| Memória da GPU | PDH `GPU Adapter Memory` | Windows | Shared Memory real; "dedicada" numa iGPU é pequena |
| RAM: Used/Available/Commit | API do Windows | `GetPerformanceInfo` | Medição real |
| RAM: frequência | SMBIOS | CIM `ConfiguredClockSpeed` | Informada pelo firmware (`reported`), **não** medida em tempo real; N/D se ausente |
| SSD: modelo, capacidade, saúde | Get-PhysicalDisk | Windows CIM | "HealthStatus" é um resumo do Windows, **não** SMART detalhado |
| SSD: temperatura/vida útil | HWiNFO | HWiNFO | Depende de o HWiNFO expor o NVMe |
| SSD: atividade, leitura, escrita | contadores | PDH `PhysicalDisk(_Total)` | Todos os discos somados |
| Qual processo "causa" a carga | CPU/GPU por processo | psutil + PDH | Classificação por limiares configuráveis (`apps.py`), suavizados; é heurística, **não** medição |
| Nome amigável | FileDescription do exe | Windows version info + regra de alias | Regras de alias editáveis (`DEFAULT_RULES`) |
| WHEA/BSOD/shutdown | Event Log | `wevtutil` (log System) | Eventos 41, 6008, 1001, 1074, WHEA etc.; classificação por tabela |
| Sleep/wake em tempo real | mensagens de energia | não implementado | Só vê sleep/wake pelo Event Log, depois do fato |

## 7. Arquivos

```
NotebookAIAgent.py        ponto de entrada (--diagnostic --list-sensors --monitor --apps)
ShowDb.py                 mostra intervalos/eventos/bateria gravados
agent/winapi.py           ctypes: memória compartilhada, bateria IOCTL, PDH, janelas
agent/collectors/         hwinfo, system (CPU/RAM/GPU/SSD), battery, foreground, processes, eventlog, static_info
agent/normalize.py        sensores HWiNFO -> chaves canônicas (regex) ou hwinfo.<cat>.<grupo>.<sensor>
agent/apps.py             nome amigável e classificação ABERTO x CAUSANDO CARGA
agent/pipeline.py         cadências e montagem do frame
agent/intervals.py        motor de intervalos (agora com suavização EMA contra picos)
agent/recorder.py, store.py, txtlog.py, events.py     persistência e eventos
tests/                    simulate.py, test_logic.py, test_cli_paths.py
```

Mudança no banco: nenhuma tabela nova; `meta` passou a guardar `design_discrepancy_logged` e `battery_snapshots` passou a ser gravada de fato (por mudança de Full Charge, mudança de estado ou 1 por hora).

## 8. Exemplo de saída esperada

Ilustrativo: **os números abaixo são exemplos do formato, não leituras do seu notebook**.

```
Battery
  Design Capacity: 58.003 mWh   [fonte: Windows Battery IOCTL]
  Full Charge Capacity: 46.800 mWh   [fonte: Windows Battery IOCTL]
  Health: 81%   [fonte: Windows IOCTL (calculado), calculated]  (Full Charge / Design)
  Capacity: N/D — a Battery API entrega mWh, nao mAh. Converter exige a tensao NOMINAL ...
  Validacao dos 58.003 mWh
    Design Capacity detectada: 58.003 mWh
    Valor de referencia informado pelo usuario: 58.003 mWh
    Diferenca: 0 mWh (0,00%)  -> coerente
...
HWiNFO:
[OK]
Sensors detected:
87
...
Overall:
READY FOR REAL-WORLD COLLECTION
```

Linha do `--monitor`:
`20:32:03 | CPU 45% | GPU 91% | RAM 12.4GB | CPU 78°C | GPU N/D | AC ON | Minecraft Java`

## 9. Ainda não implementado

- FPS/frametime (PresentMon), sessões de aplicativo gravadas em `app_sessions` (a classificação existe, a persistência por sessão não).
- Sleep/hibernate/shutdown em tempo real e detecção de desligamento sujo no próximo boot.
- Heartbeat, autostart, bandeja, GUI, instalador, `.exe`.
- Mapeamento de sensores validado contra um HWiNFO real (depende do seu `--list-sensors`).
- Recarga dos sensores do HWiNFO quando surgem novos em tempo de execução (hoje relê tudo a cada ciclo, sem cache de descoberta).
- Medição real de consumo do Agent (CPU/RAM); a estimativa anterior continua sendo só estimativa.

---
## ATUALIZACAO: LibreHardwareMonitor no lugar do HWiNFO

- Rode o **LibreHardwareMonitor** (como administrador, para ele ler CPU/SSD) e deixe-o aberto.
- O Agent le o WMI que o proprio LHM publica (`root/LibreHardwareMonitor`, classe `Sensor`); se falhar, tenta `http://localhost:8085/data.json` (ative "Remote Web Server" no LHM). Erros mostram o texto real.
- `python NotebookAIAgent.py --list-sensors` mostra tudo o que o LHM expoe e como foi mapeado.
- Flags de throttling, PL1/PL2 e Effective Clock normalmente **nao existem** no LHM: aparecem como N/D (nada e inventado).
- Correcao: PID 0 (System Idle Process), System, Registry e Memory Compression saem do ranking; o ocioso vai para `idle_cpu_pct` (metrica separada).
- `agent/collectors/hwinfo.py` ficou no projeto so como legado (nao e usado pelo pipeline).
