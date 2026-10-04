# Now

**Las noticias más importantes de los temas que tú eliges, en una app para el móvil.**

Now lee entre 10 y 20 fuentes por tema (feeds RSS y búsquedas de Google News) y agrupa los artículos que cuentan lo mismo. Cada noticia recibe un **índice de importancia de 0 a 100**. Puedes ver lo más importante de hoy, de los últimos 7 o 30 días o de unas fechas concretas.

- **Temas a tu medida.** En *Ajustes* escribes un tema («Francia», «energía solar», «Fórmula 1») y Claude busca las mejores fuentes, comprueba que sus feeds funcionan y lo añade a la barra. También puedes borrar temas.
- **Sin servidor propio ni base de datos.** GitHub Actions recoge las noticias cada 2 horas y GitHub Pages publica la app. Una función de Supabase, en un proyecto que ya tengas, gestiona los temas.
- **Se instala en el iPhone** como una app, funciona sin conexión y se refresca deslizando hacia abajo.
- **Conector para Claude.** Puedes preguntarle a Claude por tus noticias («¿qué es lo más importante hoy en energía?»).

## Instalar tu propia copia

| # | Paso | Dónde |
|---|---|---|
| 1 | **Use this template → Create a new repository** (público) | Este repositorio |
| 2 | Settings → Pages → Source: **GitHub Actions** | Tu repositorio |
| 3 | Actions → «Actualizar noticias» → **Run workflow** | Tu repositorio |
| 4 | Abre `https://TU-USUARIO.github.io/TU-REPO/` y añádela a la pantalla de inicio | iPhone (Safari) |
| 5 | Crea un token *fine-grained* con **Contents: Read and write** solo sobre tu repositorio | GitHub → Settings → Developer settings |
| 6 | Crea una clave de la API de Claude | console.anthropic.com |
| 7 | Crea la Edge Function `now` con `supabase/functions/now/index.ts` y desactiva **Verify JWT** | Supabase (cualquier proyecto) |
| 8 | Añade los secrets de la función (tabla de abajo) | Supabase → Edge Functions → Secrets |
| 9 | En tu repositorio, crea la variable `NOW_API_URL` = URL de la función | Settings → Secrets and variables → Actions → Variables |
| 10 | En la app: ⚙️ Ajustes → Conexión → pega tu clave de administración | App |
| 11 | (Opcional) Conector en Claude: `URL-de-la-función/mcp` | Claude → Settings → Connectors |

### Secrets de la función

| Secret | Valor |
|---|---|
| `NOW_SITE_URL` | `https://TU-USUARIO.github.io/TU-REPO` |
| `NOW_GITHUB_REPO` | `TU-USUARIO/TU-REPO` |
| `NOW_GITHUB_TOKEN` | El token del paso 5 |
| `NOW_ADMIN_KEY` | Una clave que inventes; la pedirá la app para cambiar temas |
| `ANTHROPIC_API_KEY` | La clave del paso 6 |
| `NOW_TIMEZONE` | Opcional. Por defecto `America/Havana` |

## Personalizar

| Archivo | Qué cambias |
|---|---|
| `topics.json` | Los temas y sus fuentes (normalmente desde los Ajustes de la app) |
| `now_config.json` → `profile_keywords` | Palabras de tus intereses; suben el índice de las noticias que las contienen |
| `now_config.json` → `authority_sources` | Medios que pesan más en el índice (agencias, prensa de referencia) |
| `.github/workflows/update.yml` | Frecuencia de actualización (por defecto, cada 2 horas de 05:00 a 23:00 en Cuba) |

## Índice de importancia

| Componente | Peso | Qué mide |
|---|---|---|
| Cobertura | 40 % | Medios distintos que publican la noticia. Las agencias cuentan 1,5 |
| Velocidad | 15 % | Parte de las menciones que llega en las primeras 24 h |
| Diversidad | 15 % | Variedad de medios y presencia de una agencia |
| Frescura | 15 % | Cuánto hace de la última mención |
| Afinidad | 15 % | Coincidencia con tus intereses (`profile_keywords`) |

## Costes

GitHub Actions y Pages son gratis en repositorios públicos, y el plan gratuito de Supabase basta para la función. Lo único de pago es la API de Claude, que solo se usa al añadir un tema: unos pocos céntimos por tema.
