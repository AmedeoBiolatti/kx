# Privacy policy

kx is a personal command-line tool for Kaggle competitions (this repository). Some setups use it with Google Drive,
through rclone and a Google OAuth app (shown as "kx rclone" on Google's sign-in page), as a shared folder between
machines.

What that app can do, and does:

- **Access:** only the `drive.file` scope: the files and folders the app itself creates in the signed-in user's own
  Google Drive (a `kx` folder). It cannot see, read or change any other file in that Drive.
- **Data:** what kx puts there is the user's own work (code archives, competition data, run outputs) and is
  exchanged only between that user's own machines. Nothing is collected, sold, shared with third parties, or sent
  anywhere else; there is no server, analytics or tracking.
- **Credentials:** the sign-in token stays in rclone's configuration file on the user's own machines.
- **Revoking:** remove the app at https://myaccount.google.com/permissions; the files it made stay in the user's
  Drive, under their control.

Questions: open an issue at https://github.com/AmedeoBiolatti/kx/issues.
