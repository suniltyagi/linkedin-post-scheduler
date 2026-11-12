# linkedin-post-scheduler

Posts a series of approved scientific posts, one a day from 12 Nov to 19 Dec 2026, to Capt (Dr) Sunil Tyagi's personal LinkedIn profile, through LinkedIn's official API. GitHub Actions runs `linkedin_post_due.py`, which posts the row of `linkedin_schedule.json` that is due, with its text from `texts.json` and its image from `images/`, and records it in `linkedin_posted.json` so nothing is posted twice.

More about the author: https://suniltyagi.in
