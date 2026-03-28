# Nomos

**Smart Legal Contract platform for vanilla Interest Rate Swaps (IRS) under the ISDA 2002 Master Agreement.**

Nomos automates the execution, monitoring, and lifecycle management of OTC derivatives contracts while maintaining strict legal hierarchy per §1(b) ISDA 2002:

> Confirmation > Schedule > Master Agreement > Code

Legal text always prevails over engine output.

---

## Structure

```
nomos/
├── backend/
│   ├── engine.py                  — IRS execution engine (calculations, EoD monitoring, oracle)
│   ├── api.py                     — FastAPI bridge (22 endpoints)
│   ├── netting_opinion_module.py  — Pre-trade netting enforceability (90+ jurisdictions)
│   ├── generate_confirmation_pdf.py — ISDA Confirmation + §12 notice PDFs
│   └── generate_contract_pdf.py   — 14-section ISDA 2002 Master Agreement PDF
├── frontend/
│   ├── login.html                 — Role selector (Client / Advisor)
│   ├── client_portal.html         — Client portfolio & payment dashboard
│   └── advisor_portal.html        — Advisor workflow & monitoring dashboard
├── outputs/                       — Generated PDFs (gitignored)
├── docs/
│   └── Nomos_Blueprint.pdf
└── tests/
    └── test_engine.py
```

---

## Quickstart

```bash
# Install dependencies
pip install -r requirements.txt

# Start the API (from nomos/)
uvicorn backend.api:app --reload --port 8000

# Open the frontend
open frontend/login.html
```

---

## Key Features

- **EURIBOR 3M oracle** — live rate from ECB SDW with ISDA 2021 fallback
- **§2(c) netting** — single net payment per calculation period
- **All 8 Events of Default** (§5(a)) and **5 Termination Events** (§5(b)) monitored
- **§2(a)(iii) circuit breaker** — payment suspension on EoD/PEoD
- **§6(e) close-out netting waterfall** — full close-out amount calculation
- **Cryptographic audit trail** — SHA-256 hash chain, tamper-evident
- **Pre-trade netting opinions** — 90+ jurisdiction database
- **PDF generation** — Confirmation, Master Agreement, and §12 notices

---

## API

Base URL: `http://localhost:8000/api`

| Endpoint | Method | Description |
|---|---|---|
| `/health` | GET | Module health check |
| `/oracle/latest` | GET | Current EURIBOR 3M rate |
| `/contracts` | GET | List all contracts |
| `/contracts` | POST | Create new IRS |
| `/contracts/{id}` | GET | Contract detail + schedule |
| `/contracts/{id}/sign` | POST | Execute Confirmation → ACTIVE |
| `/contracts/{id}/execute` | POST | Run calculation cycle |
| `/contracts/{id}/approve-pi/{period}` | POST | Approve Payment Instruction |
| `/contracts/{id}/notice` | POST | Generate §12 notice PDF |
| `/contracts/{id}/audit` | GET | Cryptographic audit trail |
| `/contracts/{id}/compliance` | GET | §3/§4 compliance summary |

---

## Legal Disclaimer

Academic prototype only. Not for production use. No legal or financial advice.
