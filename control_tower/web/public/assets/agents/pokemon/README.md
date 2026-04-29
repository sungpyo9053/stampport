# Pokemon agent sprites

Drop appropriately-licensed PNG sprites in this folder using the exact
filenames below. The Control Tower's central office (`PixelOffice` →
`AgentDesk` → `PokemonAvatar`) reads them via `import.meta.env.BASE_URL`
+ relative path, so any 1:1 replacement is picked up on the next page
load — no code change needed.

| Agent | Filename | Default fallback |
|---|---|---|
| PM | `pikachu.png` | ⚡ |
| Planner | `bulbasaur.png` | 🌱 |
| Designer | `jigglypuff.png` | 🎤 |
| Frontend | `charmander.png` | 🔥 |
| Backend | `squirtle.png` | 💧 |
| AI Architect | `eevee.png` | ✨ |
| QA | `snorlax.png` | 🛌 |
| Deploy | `meowth.png` | 📦 |

Recommended sprite spec:
- 64×64 or 96×96 transparent PNG, pixel-art friendly (`image-rendering:
  pixelated` is applied at the component level)
- Crop tight around the body — there's a soft glow/shadow drawn behind
  the image at runtime
- Avoid baked-in drop shadows; the office composites its own

If a file is missing or fails to load, `PokemonAvatar` renders the
emoji fallback + role label so the office never shows a broken-image
icon. This means the office is shippable without any binary assets.

**License note**: The Stampport Control Tower repo intentionally does
not bundle Pokemon sprite art. The mapping is purely thematic — please
source sprites you have rights to use (e.g. your own pixel art, an
internal CC0 sprite pack, or licensed assets) before going to
production.
