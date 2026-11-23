<!-- release notes for 0.5.0 -->
<!-- The top of every GitHub release page (mac-app.yml adds the list of changes below it). The line above
     names the version they are for: the release workflow refuses another (packaging/release.py), so update
     both before you release. "New in this version" is what the app's update banner shows (1500 characters). -->
**Voicestream for Mac and Windows** listens to your meetings (your microphone and the meeting's audio),
writes the transcript on your Mac, and turns it into notes and to-dos with Claude: Google
Calendar, Apple Reminders, Gmail, Google Docs, HubSpot and your own apps. French and English are
detected as people speak.

For a Mac with Apple Silicon (M1 or later) and macOS 14.2 or later. You need a Claude API key
([console.anthropic.com](https://console.anthropic.com/settings/keys)).

### New in this version

- **Client meetings, sorted.** Each meeting now says what it was: client, prospect, partner,
  internal or other, and for a client or prospect the company, the people on their side and
  where the deal stands. It shows on the meeting's page; **Change** corrects it. The
  Meetings list marks client meetings, and search finds them by company.
- **Twenty CRM.** Your client and prospect meetings file themselves in Twenty: the company and
  the people (found by name, else created), the deal moved forward but never back, its
  fields filled from your AI fields, the notes, and your to-dos as tasks. Other meetings never
  reach it. Set it up in **Settings → Your tools → Twenty CRM**; **Test** says what it found.
- **Your to-dos with AI.** At the end of a meeting, each of your to-dos says what an AI can do
  for it, with a ready prompt to paste into an AI assistant, and what is left to you. See the
  new **With AI** tab: **Copy the prompt** or **Copy all the prompts**. It costs one or two cents a meeting;
  `brain.todo_prompts = false` turns it off.

### Install

1. Download `Voicestream-…-arm64.dmg` below, under **Assets**.
2. Open it and drag **Voicestream** onto **Applications**. Then open Voicestream from your
   Applications folder.
3. macOS says it can't verify the app, because it isn't notarized by Apple. Close that message
   (don't choose Move to Trash), open **System Settings → Privacy & Security**, scroll down to
   "Voicestream was blocked…", click **Open Anyway**, enter your password and click **Open**.
   This is the last time: the versions after this one install from inside the app (**Install
   and restart** in the window, or **Install Update…** in the menu bar), without this step.
4. Follow the setup: your name and languages, speech-to-text (**On this Mac** is free and
   private; it downloads a 547 MB model), your Claude key (saved in the macOS Keychain), then
   allow **Microphone** and **System Audio Recording**.

Voicestream then lives in the menu bar; press **⌥⌘R** (or the floating button) to start or stop a
meeting. After an update, macOS may ask for the permissions again: the [README](https://github.com/bybysker/always-on-voice-stream#install-the-app-mac)
says how.

### Installer (français)

1. Téléchargez `Voicestream-…-arm64.dmg` ci-dessous, sous **Assets**.
2. Ouvrez-le et faites glisser **Voicestream** sur **Applications**, puis ouvrez Voicestream
   depuis votre dossier Applications.
3. macOS indique qu'il ne peut pas vérifier l'app, car elle n'est pas notariée par Apple. Fermez
   ce message (sans choisir « Placer dans la corbeille »), ouvrez **Réglages Système →
   Confidentialité et sécurité**, descendez jusqu'à « L'ouverture de Voicestream a été
   bloquée… », cliquez sur **Ouvrir quand même**, saisissez votre mot de passe puis cliquez sur
   **Ouvrir**. C'est la dernière fois : les versions suivantes s'installent depuis l'app
   (**Installer et redémarrer** dans la fenêtre, ou **Installer la mise à jour…** dans la barre des
   menus), sans cette étape.
4. Suivez l'assistant : votre nom et vos langues, la transcription (**Sur ce Mac** est gratuit
   et privé ; un modèle de 547 Mo est téléchargé), votre clé Claude (enregistrée dans le
   trousseau de macOS), puis autorisez le **Micro** et l'**Enregistrement audio du système**.

Voicestream vit ensuite dans la barre des menus. Rien n'est enregistré tant que vous ne démarrez
pas une réunion : **⌥⌘R** depuis n'importe quelle app, le bouton flottant, ⌘Espace →
« voicestream », ou la barre des menus. « Appel détecté — prendre des notes ? » vous le propose
quand un appel commence, sans jamais démarrer seul. Tout se règle dans Réglages → Général →
Démarrer une réunion.

### Install on Windows

1. Download `Voicestream-…-windows-x64.zip` below, under **Assets** (Windows 10 or 11, 64-bit, a
   processor with AVX2).
2. Right-click it → **Extract All…**, keep the `Voicestream` folder whole, and double-click
   `Voicestream.exe`. Windows SmartScreen warns once (the app isn't signed): **More info → Run anyway**.
3. The app opens in your browser, and its icon sits next to the clock: right-click it to start or stop
   a meeting, or to quit (the meeting in progress is saved). To update, download the new zip and
   replace the folder.

Sur Windows : téléchargez `Voicestream-…-windows-x64.zip`, **Extraire tout…**, puis double-cliquez sur
`Voicestream.exe` (SmartScreen : **Informations complémentaires → Exécuter quand même**). L'app
s'ouvre dans votre navigateur ; son icône à côté de l'horloge (clic droit) démarre ou arrête une
réunion, et quitte l'app.

### Cost

About $0.80 a day for 8 hours of meetings with speech-to-text on your Mac: that is Claude
writing the to-dos and notes. A soft cap of $3 a day by default. The app itself is free.

### Known limits

- The Promethee connection follows the MCP standard but hasn't been tried against Promethee's own
  server yet: if **Connect** or **Test** shows an error, it says why. Please send it to us.
- Not notarized by Apple, hence the Open Anyway step for a version you download yourself; updates
  installed from inside the app don't need it.
- Built and tested on GitHub's virtual Mac (the packaged app's window and menu bar, French and
  English speech through the bundled whisper.cpp), not yet on a physical Mac. If transcription
  can't keep up, the app says so: choose the faster "small" model in Settings, or cloud
  speech-to-text.
