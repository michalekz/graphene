# Telegram HTML Formátování — Průvodce pro Graphene Intel

Tento dokument popisuje možnosti HTML formátování v Telegram Bot API (`parse_mode="HTML"`).
Graphene Intel používá HTML místo Markdownu — je spolehlivější (žádné nespárované `*` entity z LLM výstupu).

---

## Podporované HTML tagy

| Tag | Alternativa | Efekt |
|-----|-------------|-------|
| `<b>text</b>` | `<strong>` | **Tučný** |
| `<i>text</i>` | `<em>` | *Kurzíva* |
| `<u>text</u>` | `<ins>` | Podtržení |
| `<s>text</s>` | `<strike>`, `<del>` | ~~Přeškrtnutí~~ |
| `<code>text</code>` | — | `Inline kód` |
| `<pre>text</pre>` | — | Blok kódu (monospace) |
| `<pre><code class="language-python">` | — | Kód se syntax highlighting |
| `<a href="URL">text</a>` | — | Odkaz |
| `<blockquote>text</blockquote>` | — | Blok citace |
| `<blockquote expandable>text</blockquote>` | — | Rozbalitelná citace (2025+) |
| `<tg-spoiler>text</tg-spoiler>` | `<span class="tg-spoiler">` | Skrytý text |
| `<tg-emoji emoji-id="...">` | — | Custom emoji |

---

## Příklady

### Základní formátování
```html
<b>HGRAF</b> — Skóre: 9/10 🟢
📰 HydroGraph files NASDAQ application
💡 Klíčový katalyzátor: <b>výpis na NASDAQ</b>
🔗 <a href="https://example.com">Zdroj: GlobeNewsWire</a>
🎯 BÝČÍ
```

### Odkaz (inline link)
```html
<a href="https://example.com/article?id=1&ref=bot">Přečíst článek</a>
```
Pozor: `&` v URL musí být `&amp;` v HTML kontextu.

### Blok kódu s jazykem
```html
<pre><code class="language-python">
def score(headline):
    return model.predict(headline)
</code></pre>
```

### Spoiler (skrytý text)
```html
<tg-spoiler>Insider: CEO kupuje akcie za $500k</tg-spoiler>
```

### Rozbalitelná citace
```html
<blockquote expandable>
Dlouhý text analytické zprávy, který uživatel může rozbalit kliknutím.
Ideální pro detailní souhrny.
</blockquote>
```

---

## Escapování speciálních znaků

V dynamickém obsahu (titulky zpráv, popisy) MUSÍ být escapovány:

| Znak | HTML entita |
|------|-------------|
| `&` | `&amp;` |
| `<` | `&lt;` |
| `>` | `&gt;` |

### Python helper (používaný v Graphene Intel)
```python
def _esc(text: str) -> str:
    """Escape HTML entities for Telegram HTML parse_mode."""
    if not text:
        return text
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
```

`html.escape(text)` ze standardní knihovny dělá totéž (escapuje i `"` → `&quot;`, ale to Telegram nepotřebuje).

---

## Co HTML NEpodporuje

- ❌ Barvy textu (`<font color="">`, CSS `style=""`)
- ❌ Nadpisy (`<h1>` až `<h6>`)
- ❌ Tabulky (`<table>`, `<tr>`, `<td>`)
- ❌ Obrázky inline (`<img>`)
- ❌ Zalomení řádku `<br>` → použij `\n`
- ❌ Formuláře, JavaScript, CSS

**Náhrada tabulek:** použij plaintext s pevnou šířkou (monospace přes `<pre>`):
```html
<pre>Ticker   Cena    Změna
HGRAF    $8.25   +43.0%
BSWGF    $0.71    +2.6%</pre>
```

---

## Limity

| Limit | Hodnota |
|-------|---------|
| Maximální délka zprávy | **4096 znaků UTF-8** |
| HTML tagy se počítají do limitu | Ano |
| Zprávy do stejného chatu | **1 zpráva/sekundu** |
| Zprávy do skupin/kanálů | **~20 zpráv/minutu** |
| Bezpečný práh pro split | 4000 znaků (Graphene Intel: `SPLIT_THRESHOLD`) |

---

## Mention uživatele/bota

```html
<a href="tg://user?id=123456789">@JanNovák</a>
```
Funguje pouze pokud uživatel je členem chatu nebo kontaktoval bota.

---

## HTML vs. MarkdownV2 — srovnání

| Aspekt | HTML | MarkdownV2 |
|--------|------|-----------|
| Počet znaků k escapování | 3 (`& < >`) | 17 (`_ * [ ] ( ) ~ \` > # + - = \| { } . !`) |
| Chyby z LLM výstupu | Vzácné | Časté (nespárované `*`, `_`) |
| Podpora tabulek | ❌ | ❌ |
| Podpora nadpisů | ❌ | ❌ |
| Blockquote expandable | ✅ | ✅ |
| Doporučení pro LLM obsah | **✅ Vhodné** | Riziko parse chyb |

**Graphene Intel používá HTML** — obsah generovaný LLM (Groq/Claude) občas obsahuje nespárované `*` (procenta, názvy firem), které by rozbily Markdown parser.

---

## Implementace v Graphene Intel

- **formatter.py**: `_esc()` pro HTML escapování, `<b>`, `<a href="">` v šablonách alertů
- **telegram.py**: `parse_mode="HTML"` jako default v `send_message()`
- **prompts.py**: Claude instrukce pro HTML výstup (`<b>tučně</b>`, žádný Markdown)

---

*Zdroj: [Telegram Bot API — HTML Style](https://core.telegram.org/bots/api#html-style)*
