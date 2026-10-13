YTTOMP4 Backend — RENDER EASY version (no folders, no coding needed)

Files in this ZIP (upload ALL of them to GitHub, at the root):
  main.py          <- the whole backend in ONE file
  Dockerfile       <- tells Render how to build it
  requirements.txt <- Python packages
  .dockerignore    <- tells Docker what NOT to copy

HOW TO FIX YOUR GITHUB (2 minutes):
1. Open your GitHub repo yttomp4-backend.
2. Click "Add file" -> "Upload files".
3. Drag ALL files from this ZIP into GitHub.
   When GitHub asks about Dockerfile (it already exists), click Replace.
4. Now your repo root must show: main.py, Dockerfile, requirements.txt.
   (No app/ folder needed in this version.)
5. Go to Render -> your service -> Manual Deploy -> Clear build cache & Deploy.
6. In Render -> Environment, set:
   API_KEY = your long random key
   PUBLIC_BASE_URL = your Render URL, e.g. https://yttomp4-backend.onrender.com
   (No trailing slash.)
7. After deploy succeeds, open https://YOUR-RENDER-URL/api/health
   You should see: {"status":"ok","version":"1.0.0"}

Then in WordPress -> YTTOMP4 Converter -> Settings:
  API Base URL = your Render URL
  API Key = same API_KEY as in Render
Save -> Test Connection.

Legal note: only convert videos you have the rights to download/convert.
