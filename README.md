# PapiClips ⚡

> **Automated AI Stream Clipper, Viral Shorts Generator & Multi-Account Scheduler**  
> *Created with ❤️ by [@papig.udda](https://www.instagram.com/papig.udda/)*

---

## 🌟 What is PapiClips?

**PapiClips** is an all-in-one AI-powered video studio that automatically transforms long gaming streams, podcasts, and videos into viral, high-retention vertical clips (9:16) for **YouTube Shorts**, **TikTok**, and **Instagram Reels**.

Unlike generic clippers that grab random chunks of footage with awkward silences, PapiClips employs a specialized **AI Editorial Team** and a **Frame-Locked Sync Engine** to guarantee that every clip starts on an irresistible hook, ends on a hilarious or shocking payoff, and has zero audio drift.

---

## 🚀 Key Features

### 1. 🕵️ Collaborative AI Editorial Team
PapiClips routes clipping tasks to the specialized AI models that perform best at each stage:
* **The Scout (Groq / Fast LLMs):** Ultra-fast, low-latency transcript evaluation and loudness spike analysis to catch explosive reactions, screams, clutch wins, and sudden chat eruptions.
* **The Creative Director (Gemini 2.0 Flash / Pro):** Crafts punchy, high-retention hooks (ALL CAPS on screen), curiosity-driven click-worthy titles, and engaging descriptions with emojis.
* **The Caption Specialist (Gemini / Groq):** Generates animated word-by-word captions with perfect punctuation and phonetic romanization for Hindi/Urdu (Hinglish: *"bhai ye kya ho gaya"*).

### 2. ⚡ Infinite Multi-API Key Failover Pool
* Never get stopped by rate limits (`429 Too Many Requests`).
* Add 2 to 3 free API keys per provider (e.g. from multiple Gmail accounts on **Google AI Studio** or **GroqCloud**).
* PapiClips automatically round-robins across healthy keys and cascades to backup models in real time without dropping your clipping pipeline.

### 3. 🎬 Frame-Accurate Audio/Video Sync & Silence Defense
* **No Video/Audio Drift:** Built with hardware-accelerated FFmpeg using `-accurate_seek` and `aresample=async=1000:first_pts=0` to ensure sound and picture are locked in microsecond sync.
* **Dead-Air Trimming:** Automatically eliminates awkward quiet spaces at the start or end of clips, ensuring high retention from second zero.
* **Speech Volume Limiter:** Auto-normalizes vocals so your clips are loud, punchy, and clear on mobile phones.

### 4. 📤 1-Click YouTube Shorts Bulk Scheduler
* Connect your YouTube channel in **1 click** ("Continue with Google" -> choose your Gmail).
* **Batch Scheduling:** Select 5 to 50 clips at once and choose:
  * **Upload All Now**
  * **Staggered Release:** Automatically posts the 1st clip now, and spaces out remaining clips by 30 mins, 1 hour, 2 hours, or 6 hours!
  * **Custom Date & Time Pickers:** Set exact publication times per clip.
* **Auto-Thumbnails:** Automatically uploads rendered clip thumbnails directly to YouTube Shorts via YouTube Data API v3.
* **Duplicate Protection Override:** Easily re-schedule or post clips multiple times without blocks.

---

## 🖥️ Quick Start (Zero Coding Required!)

### For Windows Users:

1. **Download / Clone** this repository to your computer:
   ```bash
   git clone https://github.com/your-username/papiclips.git
   ```
2. **Double-click `install.bat`**:
   * It automatically checks for Python and FFmpeg.
   * It creates your local virtual environment and installs all dependencies.
   * It downloads the English speech model for instant offline transcription.
3. **Double-click `Start PapiClips.bat`**:
   * It starts your local server and opens **http://127.0.0.1:8765/app** in your web browser automatically!

---

### For Mac & Linux Users:

```bash
# 1. Clone repository
git clone https://github.com/your-username/papiclips.git
cd papiclips

# 2. Setup Python environment (Python 3.10+)
python3 -m venv .venv
source .venv/bin/activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Setup initial config & fetch speech model
cp .env.example .env
python3 -m studio.fetch_model en

# 5. Launch PapiClips
python3 -m studio.server
```
Open **http://127.0.0.1:8765/app** in your browser.

---

## 🔑 AI Setup & Free Keys Strategy

Inside PapiClips, navigate to **Settings ⚙️ ➔ AI**:

| Role | Recommended Provider | Where to get Free Key |
|---|---|---|
| **Live Scout** | **Groq** (`llama-3.3-70b-versatile`) | [console.groq.com](https://console.groq.com) |
| **Viral Director** | **Gemini** (`gemini-2.0-flash`) | [aistudio.google.com](https://aistudio.google.com) |

> 💡 **Pro-Tip for 24/7 Unlimited Clipping:**  
> Create 2 or 3 free accounts on Google AI Studio and GroqCloud using separate Gmail addresses. Add all the keys in the **Active API Keys Pool**. PapiClips will automatically cycle between them, giving you completely free, unlimited 24/7 clipping!

---

## 📺 YouTube Channel Connection

1. In Google Cloud Console, create a Web OAuth Client ID with Authorized Redirect URI:
   `http://127.0.0.1:8765/oauth/youtube/callback`
2. Save your Client ID & Secret in PapiClips **Settings ➔ Connected Apps** (or `.env`).
3. Make sure **YouTube Data API v3** is enabled on Google Cloud.
4. From then on, any user simply clicks **"Continue with Google"** to link their channel and upload Shorts in one click!

---

## 👤 Author & Support

* **Created by:** [@papig.udda](https://www.instagram.com/papig.udda/)
* **Feedback & Feature Requests:** Send a message or report a bug directly via the in-app feedback button or on [Instagram @papig.udda](https://www.instagram.com/papig.udda/).

---

## 📄 License

This project is licensed under the **MIT License** — feel free to customize, host, and share!
