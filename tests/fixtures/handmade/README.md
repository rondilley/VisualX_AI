# Hand-made X API v2 fixtures

These JSON files are hand-made from the documented X API v2 response schema. They are not recordings of the live service. All IDs, names, and texts are made up for the tests.

The recorded responses from Slice 0 replace these files. Until then, the tests prove only the wiring and the mapping rules, not the field layout of the live service.

| File | Endpoint | What it tests |
|---|---|---|
| `users_me.json` | `GET /2/users/me` | Owner identity (ID 1000) |
| `owner_tweets_p1.json`, `owner_tweets_p2.json` | `GET /2/users/:id/tweets` | Two pages: a new post, a reply, a repost, a quote |
| `mentions_p1.json` | `GET /2/users/:id/mentions` | A reply to the owner, a mention of two users with no profile in `includes` |
| `quote_tweets_3001.json` | `GET /2/tweets/:id/quote_tweets` | A quote with no `includes.tweets` (the owner post is the target) |
| `retweeted_by_3001.json` | `GET /2/tweets/:id/retweeted_by` | Two users, no repost post ID, no repost time |
| `users_lookup.json` | `GET /2/users?ids=` | One profile and one "Not Found" error |

## Seed timelines (FR-07, plan step 6)

After the five-step run of the files above, the tie weights to the owner are: bob 2002 = 7.5, erin 2005 = 6.75, alice 2001 = 6.0, carol 2003 = 2.5, dave 2004 = 2.0, gina 2007 = 1.0. The seed files give the posts of the top contacts. The test server gives an empty page (`result_count` 0) for each other user timeline.

| File | Endpoint | What it tests |
|---|---|---|
| `seed_tweets_2002.json` | `GET /2/users/2002/tweets` | Bob, page 1 with a `next_token`: a quote of an erin post, a mention of alice and of a new user zed (2009, profile in `includes`), a reply to carol |
| `seed_tweets_2002_p2.json` | `GET /2/users/2002/tweets` with `pagination_token=s2002p2` | Page 2 for `seed_pages = 2`: a mention of carol |
| `seed_tweets_2002_since.json` | `GET /2/users/2002/tweets` with `since_id` | The incremental page: a mention of erin |
| `seed_tweets_2005.json` | `GET /2/users/2005/tweets` | Erin: a mention of yuri (2010, no profile in `includes`, so step 5 looks it up), a repost of an alice post, and the reply 4001 that is also in `mentions_p1.json` |

`users_lookup.json` also has the profile of yuri (2010). With `since_id`, the server gives an empty page for erin.
