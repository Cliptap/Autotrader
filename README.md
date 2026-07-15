# cliptap Scanner

Vigilante de trading 24/7. Escanea 5 agentes de AI-Trader cada 5 min y notifica por push al celular cuando detecta oportunidades.

## Setup

### 1. Celular
Instalar [ntfy](https://ntfy.sh) (Android/iOS) → suscribirse a `cliptap-alerts`

### 2. GitHub
Subir este repo. Ir a Settings → Secrets → Actions → agregar:

| Secreto | Valor |
|---------|-------|
| `CLIPTAP_AI_TOKEN` | tu token de AI-Trader |
| `CLIPTAP_NTFY_TOPIC` | `cliptap-alerts` |

Listo. El scanner corre solo cada 5 minutos.
