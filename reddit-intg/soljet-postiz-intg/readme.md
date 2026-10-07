## soljet-postiz-intg

Devvit app behind the Reddit channel. Reddit no longer issues self-serve Data
API keys, so posting goes through this app, which Reddit hosts. It is a thin
pass-through: the Cloud Run reddit-poster holds all the logic and calls these
external endpoints with an `X-Poster-Key` header (the app's secret `posterKey`
setting). See `src/shared/api.ts` for the request/response shapes.

| Endpoint             | Does                                                    |
| -------------------- | ------------------------------------------------------- |
| `/external/post`     | Text post, or image (fetched from `imageUrl`) + body    |
| `/external/reply`    | Comment on a post (`t3_`) or reply to a comment (`t1_`) |
| `/external/comments` | Newest comments on a post, for the poster to answer     |

Posts and comments appear as the app account (u/soljet-postiz-intg). Posts
only go to subs in the `allowedSubreddits` setting, which `make reddit-deploy`
builds from every product's `REDDIT_SUBREDDIT`; with it or `posterKey` unset
the app refuses every request (503).

## Setup

From the repo root, after `npx devvit login` once in this folder and setting
`REDDIT_SUBREDDIT` in each product's `tier.config` (the deploy generates
`REDDIT_POSTER_KEY` in `.env` if it's missing):

```
make reddit-deploy PART=--devvit-only     # upload, set key, install on each sub
make reddit-deploy PART=--install-only    # add a new sub later
```

## Commands

- `npm run upload`: clean, build, and upload a new patch version.
- `npm run dev`: playtest on your dev subreddit.
- `npm test`: types, lint, unit tests (Node 24), build.
