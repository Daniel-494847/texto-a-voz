# PDF / Word a Voz (Flask + edge-tts)

Convierte PDF o Word a MP3 y reproduce el texto resaltado palabra por palabra.

## Ejecutar en local

```powershell
pip install -r requirements.txt
python app.py
# http://127.0.0.1:5000
```

## 1. Subir el código a GitHub

1. Crea el repositorio vacío en https://github.com/new (sin README ni .gitignore).
2. En esta carpeta ejecuta:

```powershell
git init
git add .
git commit -m "Primera version: PDF/Word a MP3 con edge-tts"
git remote add origin https://github.com/TU_USUARIO/TU_REPO.git
git branch -M main
git push -u origin main
```

Si GitHub te pide credenciales, usa un **Personal Access Token** como contraseña
(GitHub > Settings > Developer settings > Personal access tokens), o instala
GitHub Desktop e inicia sesión desde la app.

## 2. Publicar la app online (Hugging Face Spaces — gratis)

1. Crea un Space en https://huggingface.co/new-space:
   - Owner: tu usuario
   - Space name: `textoavoz`
   - SDK: **Docker**
   - Visibility: Public
2. Crea un token de escritura en https://huggingface.co/settings/tokens.
3. Desde esta misma carpeta:

```powershell
git remote add hf https://huggingface.co/spaces/TU_USUARIO/textoavoz
git push hf main
```

El Space construirá el `Dockerfile` automáticamente. En 2-5 minutos tendrás la URL:

```
https://TU_USUARIO-textoavoz.hf.space
```

Para actualizar después de cambios:

```powershell
git add .
git commit -m "Cambios"
git push origin main
git push hf main
```

## Alternativa: Render conectado a GitHub (recomendado si ya usas GitHub)

El repo incluye `render.yaml`, así que Render configura todo automáticamente.

1. Sube primero el código a GitHub (paso 1).
2. Entra en https://dashboard.render.com/blueprints y pulsa **New Blueprint Instance**.
3. Conecta tu cuenta de GitHub y selecciona el repo `TU_REPO`.
4. Render detectará `render.yaml` y creará el servicio `textoavoz`.
5. Pulsa **Apply**. Espera 2-5 minutos mientras construye el Docker.
6. La URL será `https://textoavoz.onrender.com`.

Si prefieres configurarlo a mano:

1. https://dashboard.render.com/new/web-service → conecta el repo.
2. Runtime: **Docker**. Plan: **Free**.
3. Dockerfile: `./Dockerfile`.
4. El `Procfile` y el `Dockerfile` usan gunicorn con hilos para que `/progreso`
   siga respondiendo mientras se convierte.

Cada `git push origin main` vuelve a desplegar automáticamente.

## Límites importantes

- **edge-tts necesita internet saliente** (se conecta a Microsoft). Funciona en
  Hugging Face Spaces y Render; no en GitHub Pages.
- GitHub Pages **no puede ejecutar Flask**: solo sirve archivos estáticos.
- Los archivos en `uploads/` y `salidas/` son temporales en el plan gratuito:
  se pierden cuando el servicio se reinicia. La caché solo dura mientras la
  instancia siga viva.
- Los Space gratuitos se duermen tras un rato de inactividad; el primer acceso
  puede tardar unos segundos.
- Documentos muy grandes pueden superar el tiempo de espera del proxy gratuito.
  Para pruebas, usa documentos pequeños o medianos.
- XTTS (voz de `voz.wav`) no está incluido en el despliegue. Si lo necesitas,
  consulta `requirements-xtts.txt` (requiere Python 3.10-3.14 y ~2 GB de modelo).
