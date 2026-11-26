# Dengue Risk Classifier (Arduino UNO Q)

Classificador de risco de dengue com arquitetura *dual-brain*:
a MCU (STM32U585, Zephyr) lê um DHT22 e um botão e aciona LEDs; o MPU (Linux)
roda o Qwen3.5-0.8B via llama.cpp, que devolve `low`, `medium` ou `high`
com uma frase de justificativa. Uma interface Flask expõe a mesma lógica por HTTP.

## Fluxo

Sensor (MCU) -> Bridge.call("classify") -> main.py (MPU) -> llama-server (:8081)
-> JSON {risk, reason} -> código 0/1/2 -> LED (verde / amarelo / vermelho)

Fora da placa: cliente HTTP -> Flask (:7000) /classify -> mesma função classify().

## Ligações (alimentação em 3,3 V, nunca 5 V)

| Componente | Ligação |
|---|---|
| LED vermelho (risco alto) | D9 -> LED -> 220 ohm -> GND |
| LED amarelo (risco médio) | D10 -> LED -> 220 ohm -> GND |
| LED verde (risco baixo) | D11 -> LED -> 220 ohm -> GND |
| DHT22 | VCC -> 3V3, GND -> GND, DATA -> D2, pull-up 10 kohm entre DATA e VCC |
| Botão (simula presença de água) | um lado -> D3, outro -> GND |

## Estados dos LEDs

| LEDs | Significado |
|---|---|
| 3 piscadas e verde fixo | Pronto |
| Os três acesos | Inferência em andamento |
| Vermelho + amarelo | Falha na leitura do DHT22 |
| Vermelho + verde | Erro de RPC |
| Só verde / amarelo / vermelho | Risco baixo / médio / alto |

## Como rodar

1. Serviço do modelo (arquivo em `docs/llama-server.service`):
   ```bash
   sudo cp docs/llama-server.service /etc/systemd/system/
   sudo systemctl daemon-reload
   sudo systemctl enable --now llama-server.service
   curl -s http://127.0.0.1:8081/health
   ```
   Ajuste o caminho do `.gguf` no `ExecStart` se necessário.

2. Aplicação (a partir da pasta do projeto):
   ```bash
   arduino-app-cli app start .
   arduino-app-cli app logs . --follow
   ```

3. Teste do Flask a partir de outra máquina na mesma rede:
   ```bash
   curl -X POST http://<IP_DA_PLACA>:7000/classify \
     -H "Content-Type: application/json" \
     -d '{"temp_c": 30.1, "humidity_pct": 85, "standing_water": true}'
   ```

## Ajustes necessários na minha placa (em relação ao material da aula)

- `fqbn: arduino:zephyr:unoq` no `sketch/sketch.yaml`.
- Bibliotecas listadas como linhas simples (sem `dependency:`), incluindo
  MsgPack, DebugLog, ArxContainer e ArxTypeTraits.
- `RpcResult` no lugar de `RpcCall` no sketch.
- Modelo em subpasta: `~/models/Qwen3.5-0.8B-GGUF/Qwen3.5-0.8B-Q8_0.gguf`.
- O arquivo deve se chamar exatamente `python/requirements.txt`.

## Limitações observadas

- O veredito varia com a mesma entrada (temperature 0.7).
- O texto de `reason` às vezes erra o contexto (por exemplo, cita Anopheles).
- Latência de 4 a 9 s por classificação, e cerca de 25 s na primeira chamada a frio.
