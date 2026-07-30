# OCI Intake Application — Local Setup

Use these steps to run the application locally on a Mac. The current version is in this repository's `main` branch.

## 1. Install Homebrew

On Apple Silicon, also run the PATH commands printed by the installer when it finishes.

```bash
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
```

## 2. Install Git and Python

```bash
brew install git python@3.12
```

## 3. Authenticate with GitHub (if prompted)

This is only needed when GitHub requires authentication to access the repository.

```bash
brew install gh
gh auth login
```

## 4. Clone the application

```bash
git clone https://github.com/mangoproxy23/oci-intake-appv2.git
cd oci-intake-appv2
git checkout main
```

## 5. Create a virtual environment and install dependencies

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

## 6. Run the app

```bash
python3 app.py
```

Open [http://127.0.0.1:8787](http://127.0.0.1:8787) in your browser.

To run it again later:

```bash
cd oci-intake-appv2
source .venv/bin/activate
git pull origin main
python3 app.py
```
