# Extracteur de playlist YouTube

Petite app locale : tu colles un lien de playlist YouTube, elle liste le titre et le lien de chaque vidéo, avec des boutons pour copier le titre ou le lien séparément (par morceau, ou tous d'un coup).

Elle cherche aussi automatiquement les infos de chaque morceau, puis les regroupe en haut de la page pour éviter les répétitions :

- **Playlist** : ses images, son auteur et le nombre de vidéos.
- **Artistes** : une fiche par artiste, avec toutes ses photos trouvées (Genius, Deezer, TheAudioDB, Wikimedia) pour choisir et télécharger celle que tu préfères, sa description (Genius, ou Wikipédia en secours), son pays d'origine, ses genres et le nombre de ses morceaux dans la playlist.
- **Albums** : une fiche par album, avec la cover (téléchargeable), la date de sortie, le label et les genres.
- **Morceaux** : titre et lien (copiables), avec des raccourcis vers la fiche de l'artiste et de l'album.

« Exporter tout (.csv) » sort tout ça dans un fichier qui s'ouvre dans Excel.

## Pour tes amis : l'appli toute prête

Va dans la section **Releases** du dépôt GitHub (colonne de droite), télécharge le zip qui correspond à ton ordi, dézippe-le et double-clique sur `Extracteur-Playlist`. Rien à installer. Le premier lancement affiche un avertissement parce que l'appli n'est pas signée : la marche à suivre est écrite sur la page de la release.

## Lancer depuis le code (pour développer)

### Prérequis

Python 3.9 ou plus récent ([python.org/downloads](https://www.python.org/downloads/)). Sous Windows, coche « Add python.exe to PATH » pendant l'installation.

### Lancement

- **Windows** : double-clique sur `lancer-windows.bat`.
- **Mac** : double-clique sur `lancer-mac.command` (la première fois : clic droit > Ouvrir si macOS bloque le fichier).

Le premier lancement installe les dépendances (Flask et yt-dlp) dans un dossier `.venv`, puis la page s'ouvre toute seule sur http://127.0.0.1:8765. Pour arrêter, ferme la fenêtre du terminal (ou Ctrl+C).

À la main, si tu préfères : `pip install -r requirements.txt` puis `python app.py`.

## Utilisation

1. Colle un lien de playlist (`https://www.youtube.com/playlist?list=...`, ou un lien de vidéo contenant `&list=...`).
2. Clique sur **Extraire**.
3. L'icône copier à côté d'un titre ou d'un lien le copie, ou **Copier tous les titres** / **Copier tous les liens** en haut.
4. Dans une fiche artiste, clique sur une photo pour la mettre en avant ; au survol, ⤢ l'ouvre en grand et ⬇ la télécharge.

Les vidéos privées ou supprimées sont ignorées. La playlist doit être publique ou non répertoriée.

## D'où viennent les infos

Sources gratuites, sans compte ni clé : Genius (photos et description de l'artiste), Wikipédia (description en secours), Deezer (album, cover, date de sortie, label, genres, photo), MusicBrainz (pays d'origine, genres de l'album et de l'artiste), TheAudioDB (photos, genres et styles) et Wikimedia Commons (photos). Les genres de toutes ces sources sont fusionnés : ceux cités par plusieurs sources apparaissent en premier, et survoler un genre montre d'où il vient. L'app devine l'artiste et le titre à partir du titre YouTube (« Artiste - Titre ») ou de la chaîne « Artiste - Topic » ; si le titre YouTube est trop fantaisiste, le morceau affiche « Infos introuvables ». Pour les longues playlists, les infos arrivent au fur et à mesure (MusicBrainz limite à une requête par seconde, compte quelques secondes par morceau).

## Publier une nouvelle version

Onglet **Actions** du dépôt > *Fabriquer l'appli* > *Run workflow* > tape un numéro de version (ex. `v1.1.0`) > *Run workflow*. Une dizaine de minutes plus tard, une nouvelle release apparaît avec les zips Windows et Mac.

## Si ça ne marche plus

YouTube change régulièrement. Les scripts de lancement mettent yt-dlp à jour à chaque démarrage ; pour l'appli toute prête, il suffit de publier une nouvelle version (voir ci-dessus), qui embarquera le yt-dlp le plus récent.
