# TEE KAEK Αυτοματοποίηση

Τοπικό εργαλείο αυτοματοποίησης για εξουσιοδοτημένες αναζητήσεις ΚΑΕΚ στο Κτηματολόγιο / TEE.

> **ΠΡΟΣΟΧΗ:** Χρησιμοποιείτε αυτό το εργαλείο ΜΟΝΟ για νόμιμα εξουσιοδοτημένες αναζητήσεις ΚΑΕΚ για τις οποίες έχετε δικαίωμα πρόσβασης. Μην το χρησιμοποιείτε για μαζικές αναζητήσεις ή για ακίνητα που δεν σας αφορούν.

---

## Απαιτήσεις

- Python 3.10+
- Tesseract OCR (προαιρετικό, για σαρωμένα PDF)

### macOS

```bash
brew install python tesseract tesseract-lang
```

### Ubuntu/Debian

```bash
sudo apt-get install python3 python3-pip tesseract-ocr tesseract-ocr-ell poppler-utils
```

---

## Εγκατάσταση

```bash
# 1. Κλωνοποίηση / αποσυμπίεση του project
cd tee-kaek-automation

# 2. Εγκατάσταση Python dependencies
pip install -r requirements.txt

# 3. Εγκατάσταση Playwright browsers
playwright install chromium

# 4. Αντιγράψτε και ρυθμίστε το .env
cp .env.example .env
# Επεξεργαστείτε το .env και συμπληρώστε TEE_USERNAME και TEE_PASSWORD
```

---

## Εκκίνηση

```bash
python app.py
```

Ανοίξτε στο browser: **http://127.0.0.1:8080**

---

## Δομή Project

```
tee-kaek-automation/
├── app.py                      # FastAPI entry point
├── config.py                   # Ρυθμίσεις από .env
├── database/
│   └── db.py                   # SQLite schema & CRUD
├── automation/
│   ├── browser_manager.py      # Playwright helpers
│   ├── tee_auth.py             # TEE SSO login
│   ├── kaek_workflow.py        # Κύρια ροή ΚΑΕΚ
│   └── selectors.py            # Όλοι οι CSS/text selectors
├── parsing/
│   ├── pdf_parser.py           # Εξαγωγή κειμένου PDF
│   └── ocr_helper.py           # Tesseract OCR fallback
├── area_search/
│   ├── map_client.py           # Ktimatologio map integration
│   └── smart_search.py         # End-to-end smart search flow
├── api/
│   ├── routes.py               # FastAPI routes
│   └── schemas.py              # Pydantic models
├── static/
│   └── index.html              # Single-page frontend
├── downloads/                  # PDFs ανά ΚΑΕΚ
├── errors/                     # Screenshots σφαλμάτων
├── logs/                       # Log αρχεία
├── .env.example
├── requirements.txt
└── README.md
```

---

## Χαρακτηριστικά

### Dashboard
- Στατιστικά επεξεργασίας σε πραγματικό χρόνο μέσω WebSocket
- Εκκίνηση / Διακοπή batch επεξεργασίας

### Διαχείριση ΚΑΕΚ
- Προσθήκη μεμονωμένου ΚΑΕΚ
- Εισαγωγή λίστας (ένα ανά γραμμή ή ελεύθερο κείμενο)
- Δημιουργία δοκιμαστικών ΚΑΕΚ (μόνο για UI testing)
- Retry αποτυχημένων ΚΑΕΚ (έως 2 φορές)

### Smart Search (Αναζήτηση Περιοχής)
1. Ανακάλυψη ΚΑΕΚ από public WFS/REST endpoints του Κτηματολογίου
2. Interception network calls του χάρτη `maps.ktimatologio.gr`
3. Φιλτράρισμα βάσει έκτασης (τ.μ. ή στρέμματα με ανοχή ±%)
4. Σύνδεση στο TEE και λήψη PDF μόνο για τα ΚΑΕΚ που πληρούν τα κριτήρια
5. Ανάλυση PDF: εμβαδόν, κτίσμα, βάρη, τύπος ιδιοκτησίας
6. Ταξινόμηση: Χωρίς βάρη / Με βάρη / **Άγνωστο**

> **Κανόνας ασφαλείας:** Ένα ΚΑΕΚ χαρακτηρίζεται "Χωρίς βάρη" ΜΟΝΟ όταν το PDF το επιβεβαιώνει ρητά. Αν λείπουν πληροφορίες → "Άγνωστο".

### PDF Parsing
- pdfplumber (πρωτεύον — ψηφιακά PDF)
- PyMuPDF (fallback)
- Tesseract OCR (εφεδρικό — σαρωμένα PDF)
- Claude AI (προαιρετικό, με `ANTHROPIC_API_KEY`)

### Export
- CSV, Excel (.xlsx)

---

## Δομή Downloads

```
downloads/
  {KAEK_sanitized}/
    {KAEK}_perigrafiki_vasi.pdf
    {KAEK}_xoriki_vasi.pdf

errors/
  {KAEK}/
    {step}_{timestamp}.png   ← Screenshots σφαλμάτων
```

---

## Ενημέρωση Selectors

Το αρχείο `automation/selectors.py` περιέχει όλους τους CSS/text selectors.
Αν η αυτοματοποίηση αποτυγχάνει, ανοίξτε το portal με DevTools (F12) →
Network/Elements και ενημερώστε τα `primary` fields για κάθε `SelectorSet`.

---

## Ασφάλεια

- Τα credentials αποθηκεύονται **μόνο** στο `.env` (ποτέ στον κώδικα)
- Το `.env` είναι στο `.gitignore`
- Passwords **δεν εκτυπώνονται ποτέ** στα logs
- Το εργαλείο εκτελείται **τοπικά μόνο** (127.0.0.1)
- Φυσικές καθυστερήσεις μεταξύ αιτημάτων (ρυθμιζόμενο)

---

## API Docs

Διαθέσιμο στο: http://127.0.0.1:8080/api/docs
