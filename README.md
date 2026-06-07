# Mikrotik SMS Reader

Simple Python project to connect to a MikroTik router and read incoming SMS messages.

Setup

1. Create a virtual environment and activate it:

```bash
python3 -m venv venv
source venv/bin/activate
```

2. Install dependencies:

```bash
pip install --upgrade pip
pip install -r requirements.txt
```

3. Copy `.env.example` to `.env` and fill in `MIKROTIK_HOST`, `MIKROTIK_USER`, `MIKROTIK_PASS`.

Run

```bash
source venv/bin/activate
python src/main.py
```

Notes
- Uses `routeros_api` to talk to the RouterOS API.
- The script prints messages from the `/tool/sms/inbox` resource.
