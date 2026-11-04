# Acknowledgments

VisualX_AI is a derivative project. Ron Dilley made it to learn how the tools of other persons work. The good ideas in it come from the persons and projects on this page. VisualX_AI copies no code from them. The errors are ours.

## The first sources of ideas

### Daniel Card (@UK_Daniel_Card, @mRr3b00t)

Daniel Card built an X network map and showed it in public posts from 2026-10-19 to 2026-11-01 ([x.com/UK_Daniel_Card](https://x.com/UK_Daniel_Card)). These posts started this project. The VisualX_AI page follows the look of that map:

- The dark canvas with a dim hexagonal grid.
- The shaded nodes, the thin links with one color for each interaction type, and the bright links when you move the mouse onto an account.
- The hover card, the legend, the node counter, and the zoom buttons.
- The gold-on-dark-brown controls.
- Graph playback, with a timeline, a clock, and counters.
- The dashboard with cards for accounts by state, verification type, and interaction type.

That code is not public, so VisualX_AI uses only ideas from the images and videos. The notes are in `background/2026-11-03_reference_ui_uk_daniel_card.md`.

### Jacob Posel: x-network-map

[poseljacob/x-network-map](https://github.com/poseljacob/x-network-map) (MIT, Copyright (c) 2026 Jacob Posel) gave VisualX_AI these ideas:

- The data contract (`interactions.json`, `profiles.json`). `visualx export --format xnm` writes these shapes.
- The tie-strength formula and the start weights for each interaction type.
- The rule "never invent a node": an account with no profile does not get made-up values.
- The rule "stop before paid calls", which became the cost plan and the spend cap.
- Bio topics from keywords.

## Other tools that were references

| Tool | What VisualX_AI uses from it |
|---|---|
| [DocNow twarc](https://github.com/DocNow/twarc) and [twarc-network](https://github.com/DocNow/twarc-network) | The rules that give each post an interaction type. For example, the "RT @user" text in a repost is not a mention. |
| [twitter-explorer](https://github.com/pournaki/twitter-explorer) (Armin Pournaki) | The idea to hide data about small accounts (`render --redact`). Idea only. |
| [Botometer and Hoaxy](https://osome.iu.edu/) (OSoMe, Indiana University) | The feature model for bot signals (Varol and others, 2017). VisualX_AI writes its own rules, because Botometer X cannot score accounts made after 2023-05-31. |
| [Gephi](https://github.com/gephi/gephi) and [Gephi Lite](https://github.com/gephi/gephi-lite) | The GEXF export format (`visualx export --format gexf`). |
| [Community Archive](https://github.com/TheExGenesis/community-archive), [map-tpot](https://github.com/anantham/map-tpot), [cosmos.gl](https://github.com/cosmosgl/graph), [force-graph](https://github.com/vasturiano/force-graph), NodeXL | Examples that the research examined. They helped to set the scope and the fallback plans. |

## Research

| Source | Where VisualX_AI uses it |
|---|---|
| Traag, Waltman, and van Eck (2019). "From Louvain to Leiden: guaranteeing well-connected communities." *Scientific Reports* 9, 5233. | The Leiden communities, with 20 runs and a stability value. |
| Jacomy, Venturini, Heymann, and Bastian (2014). "ForceAtlas2, a continuous graph layout algorithm for handy network visualization designed for the Gephi software." *PLOS ONE* 9(6), e98679. | The force layout in the page. |
| Pacheco and others (2021). "Uncovering Coordinated Networks on Social Media: Methods and Case Studies." ICWSM 2021. | The `co_action` detector. |
| Varol and others (2017). "Online Human-Bot Interactions: Detection, Estimation, and Characterization." ICWSM 2017. | The profile and behavior signal rules. |
| Rauchfleisch and Kaiser (2020). "The false positive problem of automatic bot detection in social science research." *PLOS ONE* 15(10), e0241045. | The neutral state names, and the cause shown for each signal. |
| Okabe and Ito. "Color Universal Design." | The colors that persons with color-vision deficiencies can see. |

The full list of sources is in `docs/2026-11-02_X_Network_Analysis_Research.md`.

## Software in the tool

| Package | License | Use |
|---|---|---|
| [Sigma.js](https://github.com/jacomyal/sigma.js) 4.0.4 | MIT | The WebGL graph in the page |
| [graphology](https://github.com/graphology/graphology) 0.26.0 and graphology-library 0.8.0 | MIT | The graph model, ForceAtlas2, and paths in the page |
| events, graphology-utils, mnemonist, obliterator | MIT | Dependencies of graphology |
| [python-igraph](https://github.com/igraph/python-igraph) | GPL-2.0-or-later | Metrics and layout |
| [leidenalg](https://github.com/vtraag/leidenalg) | GPL-3.0 | Leiden communities |
| httpx, keyring, Jinja2, PyYAML, Typer | BSD, MIT | HTTP, the Windows Credential Manager, the page template, configuration, and the command line |
| pytest, Playwright | MIT, Apache-2.0 | Tests |

The license files of the JavaScript packages are in `src/visualx/render/vendor/licenses/`.

## AI tools

Claude Code (Anthropic) wrote most of the code and the documents. OpenAI, Gemini, Mistral, and Claude models also examined the architecture (`.ai-reviews/`). xAI Grok `x_search` is an optional data source.

## Data

The X API v2 supplies the data. VisualX_AI is not related to X Corp.

Thank you to each of these persons and teams for the work that they made public.
