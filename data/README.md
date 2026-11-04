# Fault code and standard value data

## dtc_generic.json and dtc_details.json.gz

Titles, descriptions, common causes, symptoms and flags for generic fault codes, built from [OBDex](https://github.com/foerbsnavi/OBDex). Only the English text is kept.

- **Licence:** CC0 1.0, a public-domain dedication, full text in [LICENSE-OBDex](LICENSE-OBDex)
- **Covers:** generic codes only (P0, P2, P3, B0, C0, U0 and U3)
- **Doesn't cover:** manufacturer-specific codes, whose meaning depends on the carmaker

OBDex is reviewing its codes one by one, so check important diagnoses against a service manual.

## standard-pids.json

Definitions of standard OBD-II live values, used only for values Fahis has no tested decoder of its own for.

- **Source:** [OBDb/SAEJ1979](https://github.com/OBDb/SAEJ1979), `signalsets/v3/default.json`, commit `06690177d2d5e11dbf82566297128d673b5a03a7`
- **Authors:** OBDb editors and contributors
- **Licence:** [CC BY-SA 4.0](https://creativecommons.org/licenses/by-sa/4.0/), full text in [../profiles/LICENSE](../profiles/LICENSE)
- **Changes:** the JSON was saved unchanged
