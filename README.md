# RMv8 Agent — Groq + Alpha Vantage

## Keys necesarias (ya tienes ambas)
ALPHA_VANTAGE_KEY=TU_KEY_AQUI
GROQ_KEY=TU_KEY_GROQ_AQUI

---

## Deploy en Railway — paso a paso desde iPhone

### 1. Crear repo en GitHub (desde iPhone)
1. Abre github.com en Safari
2. Login → botón "+" → New repository
3. Nombre: rmv8-agent → Public → Create repository
4. Botón "uploading an existing file"
5. Sube los 4 archivos: main.py, index.html, requirements.txt, nixpacks.toml
6. Commit changes

### 2. Deploy en Railway
1. Abre railway.app en Safari
2. Login with GitHub
3. New Project → Deploy from GitHub repo
4. Selecciona rmv8-agent
5. Espera ~3 minutos (barra de progreso)

### 3. Variables de entorno (LAS KEYS)
En Railway → tu proyecto → Variables → Raw Editor → pega esto:

ALPHA_VANTAGE_KEY=3BTJKIKE9E7CV2B3
GROQ_KEY=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

→ Update Variables → Railway redeploya automático

### 4. Obtener tu URL
Railway → tu proyecto → Settings → Domains → Generate Domain
Te da algo como: rmv8-agent-production-xxxx.up.railway.app

### 5. Usar desde iPhone
1. Abre esa URL en Safari
2. Pega la URL completa (con https://) en el campo de configuración
3. Guardar y conectar
4. Pulsa "▶ Analizar"

### Añadir a pantalla de inicio (iPhone)
Safari → Compartir → Añadir a pantalla de inicio
Se abre como app nativa sin barra del browser.

---

## Límites gratuitos

Alpha Vantage free: 25 requests/día
- Análisis manual: 1 request por análisis → 25 análisis/día (suficiente)
- Auto cada 30min: 48 requests/día → supera límite (usar con moderación)

Groq free: 1000 requests/día, 14400 tokens/minuto
- Completamente suficiente para uso normal

Railway free: $5 crédito/mes
- Este backend usa ~$0.50/mes → sobra

---

## Archivos
main.py         → Backend FastAPI (Groq + Alpha Vantage + cálculos)
index.html      → Frontend optimizado para iPhone
requirements.txt → Dependencias Python
nixpacks.toml   → Configuración Railway
