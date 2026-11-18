# GameBanana Tracker — version GitHub

Les relevés tournent chaque jour sur les serveurs de GitHub, et le tableau de bord est publié en ligne : tu peux le consulter de n'importe où, téléphone compris. Ton PC n'a plus besoin d'être allumé.

Ton tableau de bord sera à l'adresse **https://themonkgh.github.io/gb-tracker/** (si le dépôt s'appelle `gb-tracker`).

> ⚠️ Le dépôt doit être **public** pour que tout reste gratuit. Tout le monde peut donc voir ton tableau de bord et tes relevés. Ce ne sont que des chiffres déjà publics sur GameBanana.

## Installation (10 min)

### 1. Récupérer tes relevés déjà faits sur ton PC (facultatif)

Si tu utilisais déjà la version PC et que tu veux garder ton historique :

1. Copie ton fichier `gb_stats.db` dans ce dossier.
2. Ouvre un terminal dans ce dossier et lance : `py gb_tracker.py exporter-csv gb_stats.db`
3. Un dossier `data/` apparaît : il contient tout ton historique en fichiers texte.

### 2. Créer le dépôt

1. Va sur **https://github.com/new**.
2. Nom : `gb-tracker`. Visibilité : **Public**. Ne coche rien d'autre. Clique sur *Create repository*.

### 3. Envoyer les fichiers

1. Sur la page du dépôt vide, clique sur le lien **uploading an existing file**.
2. Glisse-dépose **le contenu** de ce dossier : `gb_tracker.py`, `dashboard_template.html`, `config.json`, `.gitignore`, `LISEZMOI.md`, le dossier `.github` et, si tu l'as créé, le dossier `data`.
   **N'envoie pas `gb_stats.db`** : la version GitHub n'en a pas besoin.
3. Clique sur *Commit changes*.

**Si le dossier `.github` n'a pas été envoyé** (vérifie qu'il apparaît bien dans le dépôt) : clique sur *Add file → Create new file*, tape comme nom `.github/workflows/releve.yml` (les `/` créent les dossiers tout seuls), colle dedans le contenu du fichier `.github/workflows/releve.yml` de ce dossier, puis *Commit changes*.

### 4. Activer la publication du tableau de bord

Dans le dépôt : **Settings → Pages → Build and deployment → Source : GitHub Actions**.

### 5. Lancer le premier relevé

Onglet **Actions → Relevé GameBanana → Run workflow → Run workflow**. Au bout de 3 à 5 minutes, ton tableau de bord est en ligne.

Ensuite, le relevé se lance tout seul chaque jour vers 4h37 (heure de Paris en hiver, 5h37 en été). GitHub peut retarder, voire sauter, une tâche planifiée : un relevé de secours passe donc vers 16h37 (17h37 en été). Il ne fait rien si celui du matin a réussi, et prend le relais sinon.

### 6. Arrêter la version PC

Pour ne pas faire deux relevés par jour : ouvre le **Planificateur de tâches** Windows, trouve **GameBanana Tracker** et supprime-la.

## Au quotidien

- **Tableau de bord** : l'adresse ci-dessus, mise à jour après chaque relevé.
- **Si un relevé échoue** (GameBanana bloque les serveurs de GitHub, panne...) : le relevé apparaît en rouge dans l'onglet Actions, un encadré rouge s'affiche sur le tableau de bord, et GitHub t'envoie en principe un e-mail. Ce qui a déjà été relevé est quand même enregistré et publié.
- **Modifier un réglage** : ouvre `config.json` sur GitHub, clique sur le crayon, modifie, puis *Commit changes*. Le changement est pris en compte au relevé suivant.
- **Changer l'heure** : dans `.github/workflows/releve.yml`, lignes `cron:`. Le format est *minute heure*, en heure UTC. Si tu changes l'heure du relevé de secours, change-la aussi à la ligne `if [ "$PLANIFICATION" = ...` du même fichier.

## Où sont les données

| Emplacement | Contenu |
|---|---|
| `data/snapshots/AAAA/AAAA-MM-JJ.csv` | les chiffres du jour (1 fichier par jour, jamais modifié ensuite) |
| `data/categories/AAAA/AAAA-MM-JJ.csv` | les mods de chaque catégorie ce jour-là |
| `data/mods.csv` | nom, catégorie, date de sortie de chaque mod |
| `data/runs.csv` | le bilan de chaque relevé |

Ce sont des fichiers texte : tu peux les ouvrir dans Excel. Le dépôt grossit d'environ 20 Ko par jour.

## En cas de problème

- **L'étape « Enregistrer les données » échoue avec « Permission denied »** : *Settings → Actions → General → Workflow permissions →* coche **Read and write permissions**, puis relance.
- **L'étape « Publier le tableau de bord » échoue** : vérifie l'étape 4 (Source : GitHub Actions).
- **Relevé « bloqué » tous les jours** : GameBanana refuse probablement les serveurs de GitHub. Dans ce cas, reviens à la version PC.
- **Plus aucun relevé depuis longtemps** : GitHub désactive les tâches planifiées des dépôts publics sans activité depuis 60 jours. Les relevés enregistrés chaque jour devraient compter comme de l'activité. Si ça arrive quand même : onglet Actions → *Enable workflow*.
