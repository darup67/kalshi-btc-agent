# PAUSED October 8, 2026 (user request)
The Kalshi BTC 15-minute caller and its weekly recalibration job are unloaded; their plists were renamed to .plist.disabled in ~/Library/LaunchAgents.
Resume: for l in kalshibtc kalshibtc.recal; do mv ~/Library/LaunchAgents/com.dhruv.$l.plist.disabled ~/Library/LaunchAgents/com.dhruv.$l.plist; launchctl load ~/Library/LaunchAgents/com.dhruv.$l.plist; done; then rm this file.
