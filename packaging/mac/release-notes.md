<!-- The top of every GitHub release page (mac-app.yml adds the list of changes below it).
     Keep it true for the version you tag. -->
**Voicestream for Mac** listens to your meetings (your microphone and the meeting's audio),
writes the transcript on your Mac, and turns it into notes and to-dos with Claude: Google
Calendar, Apple Reminders, Gmail, Google Docs, HubSpot and your own apps. French and English are
detected as people speak.

For a Mac with Apple Silicon (M1 or later) and macOS 14.2 or later. You need a Claude API key
([console.anthropic.com](https://console.anthropic.com/settings/keys)).

### Install

1. Download `Voicestream-…-arm64.dmg` below, under **Assets**.
2. Open it and drag **Voicestream** onto **Applications**. Then open Voicestream from your
   Applications folder.
3. macOS says it can't verify the app, because it isn't notarized by Apple. Close that message
   (don't choose Move to Trash), open **System Settings → Privacy & Security**, scroll down to
   "Voicestream was blocked…", click **Open Anyway**, enter your password and click **Open**.
   This is needed once for each version.
4. Follow the setup: your name and languages, speech-to-text (**On this Mac** is free and
   private; it downloads a 547 MB model), your Claude key (saved in the macOS Keychain), then
   allow **Microphone** and **System Audio Recording**.

Voicestream then lives in the menu bar. After an update, macOS may ask for the permissions
again: the [README](https://github.com/bybysker/always-on-voice-stream#install-the-app-mac)
says how.

### Installer (français)

1. Téléchargez `Voicestream-…-arm64.dmg` ci-dessous, sous **Assets**.
2. Ouvrez-le et faites glisser **Voicestream** sur **Applications**, puis ouvrez Voicestream
   depuis votre dossier Applications.
3. macOS indique qu'il ne peut pas vérifier l'app, car elle n'est pas notariée par Apple. Fermez
   ce message (sans choisir « Placer dans la corbeille »), ouvrez **Réglages Système →
   Confidentialité et sécurité**, descendez jusqu'à « L'ouverture de Voicestream a été
   bloquée… », cliquez sur **Ouvrir quand même**, saisissez votre mot de passe puis cliquez sur
   **Ouvrir**. À refaire une fois pour chaque version.
4. Suivez l'assistant : votre nom et vos langues, la transcription (**Sur ce Mac** est gratuit
   et privé ; un modèle de 547 Mo est téléchargé), votre clé Claude (enregistrée dans le
   trousseau de macOS), puis autorisez le **Micro** et l'**Enregistrement audio du système**.

### Cost

About $0.80 a day for 8 hours of meetings with speech-to-text on your Mac: that is Claude
writing the to-dos and notes. A soft cap of $3 a day by default. The app itself is free.

### Known limits

- Not notarized by Apple, hence the Open Anyway step for each version.
- Built and tested on GitHub's virtual Mac (the packaged app's window and menu bar, French and
  English speech through the bundled whisper.cpp), not yet on a physical Mac. If transcription
  can't keep up, the app says so: choose the faster "small" model in Settings, or cloud
  speech-to-text.
