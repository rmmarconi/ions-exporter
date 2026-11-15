# ions-exporter

Exporter Prometheus para microinversores **Intelbras IONS-2K M1**, lidos direto por
**Modbus TCP** pelo Wi-Fi do próprio micro. Sem nuvem, sem aplicativo, sem hardware extra.
Um único arquivo Python, só com a biblioteca padrão.

[Read in English](README.md)

> Projeto não oficial, sem relação com a Intelbras. O mapa de registradores foi levantado
> por engenharia reversa e só foi testado no IONS-2K M1 com firmware `MICROV11`.

## O que ele entrega

- Potência CA e CC, energia total e energia de hoje
- Tensão, corrente e potência **por placa** (4 entradas) e energia de cada placa no dia
- Tensão, corrente e frequência da rede, fator de potência, potência aparente
- Temperaturas, estado, número de série, modelo e firmware
- Dashboard Grafana pronto ([grafana/ions-solar.json](grafana/ions-solar.json))

## Requisitos

- Micro acessível pelo host do exporter na **porta TCP 502** (Modbus TCP, unit id 1, sem senha).
  Reserve um IP fixo para cada micro no roteador (DHCP).
- Python 3.9+ ou Docker.

## Uso rápido

```bash
# uma leitura decodificada e sai
ONCE=1 IONS_TARGETS=micro1=192.168.1.50 python3 exporter.py

# exporter rodando (métricas em http://localhost:9596/metrics)
IONS_TARGETS=micro1=192.168.1.50,micro2=192.168.1.51 python3 exporter.py
```

Com Docker: edite os IPs em [docker-compose.yml](docker-compose.yml) e rode `docker compose up -d`.
Job do Prometheus em [prometheus.yml](prometheus.yml).

No Grafana, importe [grafana/ions-solar.json](grafana/ions-solar.json), escolha o datasource
Prometheus e preencha a variável **Tariff per kWh** (R$/kWh com impostos) para ver a economia.
O gráfico do ano precisa de retenção de pelo menos um ano no Prometheus
(`--storage.tsdb.retention.time=400d`).

## Configuração

| Variável       | Padrão             | Descrição |
|----------------|--------------------|-----------|
| `IONS_TARGETS` | (obrigatória)      | pares `nome=ip` separados por vírgula |
| `SITE`         | `ions`             | rótulo `site` de todas as séries |
| `PORT`         | `9596`             | porta do `/metrics` |
| `IONS_POLL`    | `30`               | segundos entre leituras de cada micro |
| `IONS_TIMEOUT` | `5`                | timeout de conexão/leitura (s) |
| `IONS_MAX_AGE` | `300`              | por quanto tempo servir a última leitura boa se o micro parar de responder (s) |
| `IONS_RATED_W` | `2000`             | potência nominal informada em `solar_info` |
| `STATE_FILE`   | `ions-state.json`  | estado da energia diária (`/state/ions-state.json` na imagem Docker) |
| `TZ`           | do sistema         | fuso usado para virar o dia |

O micro não guarda energia do dia nem energia por placa. O exporter calcula **hoje** como
contador total − primeira leitura do dia, e **por placa** integrando tensão × corrente. Esse
estado fica em `STATE_FILE` e sobrevive a reinícios.

À noite o micro desliga junto com o Wi-Fi: `ions_up` vai a 0 e as séries somem até o sol voltar.
Isso é normal.

Métricas e mapa de registradores completos: veja o [README em inglês](README.md#metrics).

## Licença

[GPL-3.0](LICENSE)
