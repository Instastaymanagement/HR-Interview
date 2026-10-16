# HR Interview

Screens job candidates over text (Quo), several at a time, and closes out the vacancy once someone is hired.

It works from the **Vacancies** board in the *Operations Automated* monday.com workspace. Each **group** on that board is one vacancy (for example *Part-Time Cleaner – London ON*), and each **item** is a candidate with a Phone, Hiring Stage and *Flags / Ask on Call*.

## What it does every 2 minutes

1. **First text:** every candidate whose **Hiring Stage** is *Shortlist – Call* and who hasn't been texted yet gets a first message. Claude writes it from the role and the candidate's CV notes. The stage changes to **Contacted**.
2. **Screening:** when a candidate replies, Claude reads the whole thread and texts back the next question. It covers what the role needs (from `roles.json`) and the points in that candidate's *Flags / Ask on Call*: car and licence, availability, distance from London, experience. The stage changes to **Screening**.
3. **Decision:** once Claude knows enough, it sends a short, friendly sign-off ("someone from the team will be in touch"). It sets the stage to **Good Fit** or **Not a Fit**, writes its reasoning in **Screening Notes**, and posts it as an update on the item. Candidates are never told they're not a fit at this point. If a candidate says they're no longer interested, they're set to **Withdrawn**. If they confirm a car, *Car / Licence* is updated.
4. **Hired:** when you set anyone in a vacancy to **Hired**, the automation stops screening that vacancy. Everyone else in it who was texted gets a message: the position is filled, thank you, and we'll be in touch if another opening suits them. They're set to **Not Selected**. Candidates who were never texted get no message.

Other rules:

- **When it stops texting someone:** after **Good Fit**, **Not a Fit**, **Withdrawn**, **Interview Booked** or **Trial Clean**, the automation leaves that candidate to you.
- **Answering in Quo yourself:** if you reply to a candidate in Quo, the automation doesn't reply on top of you. It carries on from the candidate's next message.
- **Send hours:** texts only go out between 9 AM and 8 PM local time. Replies that arrive at night are answered the next morning.
- **Message limit:** at most 8 texts per candidate. The last one has to include a decision.
- **Which number sends:** Canada vacancies text from the London Quo line (+1 226-781-3731), USA vacancies from the Boston line (+1 857-353-8919). The country comes from the vacancy's group name, e.g. "London ON".
- **What Claude won't do:** it never invents pay, hours or start dates. It says the team will cover those on a call. If a candidate sincerely asks whether they're texting a person, it says the messages come from an automated assistant and a team member reviews every conversation.

## Adding a vacancy

1. Add a group to the Vacancies board named like *Role – City Province* (e.g. *Cleaner – Boston MA*), and add the candidates with their phone numbers.
2. Add an entry to `roles.json` whose key appears in the group name, with the role's `description`, the `must_know` points, and what Claude `can_share` if asked.
3. Set the candidates you want texted to *Shortlist – Call*.

## Setup

Repository secrets (**Settings → Secrets and variables → Actions**). These can be the same values as in Task-Management:

| Secret | Notes |
|---|---|
| `QUO_API_KEY` | Quo API key |
| `MONDAY_API_TOKEN` | monday.com API token |
| `ANTHROPIC_API_KEY` | Claude API key |
| `ANTHROPIC_WORKSPACE_ID` | Only if the key isn't scoped to a workspace |

**Every 2 minutes:** GitHub's own timer runs this at most every 5 minutes, and often late. Add a second job on cron-job.org, set up the same way as the Task-Management one:

- URL: `https://api.github.com/repos/Instastaymanagement/HR-Interview/actions/workflows/hr-interview.yml/dispatches`
- Every 2 minutes, method POST, the same 4 headers (your token needs **Actions: Read and write** on HR-Interview too)
- Body: `{"ref":"main","inputs":{"dry_run":"false"}}`

**Test first:** go to Actions → *Screen candidates over Quo* → Run workflow. With **dry_run** ticked, the run page lists every text it would send, and nothing is sent.

## Tests

```
pip install -r requirements.txt
python -m unittest discover -s tests
```
