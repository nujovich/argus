---
name: argus-request-compute
description: >
  Use ANTES de elegir un modelo de inferencia para un job con valor económico.
  Declarás el revenue esperado y el burn proyectado; Argus decide el tier de
  compute (ULTRA / BASE / REJECT) y te devuelve el modelo permitido + un auth
  token que DEBÉS reenviar en tus llamadas a Nemotron. No elijas modelo de
  memoria: pedí el tier primero y usá exactamente el que Argus asigne.
---

# argus-request-compute

Antes de correr un job que produce ingreso, declarás su economía a Argus y este
decide qué modelo de Nemotron podés usar según el margen. Evita quemar un modelo
caro en un job de bajo margen (o negativo). El agente NO elige el modelo: Argus
lo asigna.

## Cuándo usarla
- El job tiene un `expected_revenue_usd` conocido o estimable.
- Vas a elegir entre un modelo premium (ultra) y uno base.
- SIEMPRE antes de la inferencia, nunca después.

## Cómo pedir el tier

Hacé exactamente este POST (copialo, no lo armes de memoria):

```bash
curl -sS -X POST http://localhost:9120/api/plugins/argus/sim/compute \
  -H 'Content-Type: application/json' \
  -d '{
    "job_id": "<id-del-job>",
    "cost_center_id": "default",
    "expected_revenue_usd": <revenue>,
    "projected_burn_usd": <burn-proyectado>
  }'
```

## Qué te devuelve

En ALLOW (tier ultra o base):

```json
{
  "result": {
    "action": "allow",
    "tier": "ultra",
    "model": "nvidia/nemotron-3-ultra-550b-a55b",
    "compute_budget_usd": 15.0,
    "expected_margin_usd": 185.0,
    "auth_token": "<token>",
    "expires_in": 60,
    "allocation_id": 1
  }
}
```

En REJECT (margen insuficiente):

```json
{
  "result": {
    "action": "block",
    "message": "Argus rejected compute: negative_margin ...",
    "verdict": "TIER_REJECT",
    "expected_margin_usd": -2.0
  }
}
```

## Qué hacer con la respuesta

- `action: "allow"` -> usá EXACTAMENTE el `model` que vino en la respuesta. NO uses
  otro modelo.
- **Guardá el `auth_token`** y reenvialo en la metadata de tu llamada a Nemotron,
  en el campo `metadata.argus_auth_token`. Sin el token, la capa de integridad de
  compute marca la corrida como violación al cotejarla contra telemetry.
- El token vence en `expires_in` segundos (60). Pedí el tier justo antes de
  inferir, no con anticipación.
- `action: "block"` -> NO corras el job. El margen no lo justifica. Reportá el
  `message`.

## Ejemplo

Job de comisión, ingreso $200, burn proyectado $15:

```bash
curl -sS -X POST http://localhost:9120/api/plugins/argus/sim/compute \
  -H 'Content-Type: application/json' \
  -d '{
    "job_id": "mermelada-commission-001",
    "cost_center_id": "default",
    "expected_revenue_usd": 200.0,
    "projected_burn_usd": 15.0
  }'
```

-> `allow`, tier `ultra`, modelo `nvidia/nemotron-3-ultra-550b-a55b`, token de 60s.
Usás ese modelo y reenviás el token en `metadata.argus_auth_token`.
