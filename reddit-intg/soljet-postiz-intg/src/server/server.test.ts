import assert from 'node:assert/strict'
import {createServer, type IncomingMessage} from 'node:http'
import type {AddressInfo, Server} from 'node:net'
import {after, before, beforeEach, test} from 'node:test'
import {
  type Context,
  media,
  reddit,
  runWithContext,
  settings,
} from '@devvit/web/server'
import {
  type CommentsRsp,
  Endpoint,
  type ErrorRsp,
  type PostReq,
  type PostRsp,
  posterKeyHeader,
  type ReplyRsp,
} from '../shared/api.ts'
import {checkPosterKey, maxBodyBytes, onReq} from './server.ts'

const key = 'test-key'
let server: Server
let serverURL: string
let submitted: Record<string, unknown>[] = []
let posterKey: string | undefined = key
let allowedSubs: string | undefined = 'test, r/Arboryx'
let submitFail = false

// biome-ignore lint/suspicious/noExplicitAny: test doubles for SDK clients.
const r = reddit as any
// biome-ignore lint/suspicious/noExplicitAny: test doubles for SDK clients.
const s = settings as any
// biome-ignore lint/suspicious/noExplicitAny: test doubles for SDK clients.
const m = media as any

before(async () => {
  s.get = async (name: string) =>
    name === 'posterKey' ? posterKey : allowedSubs
  m.upload = async () => ({mediaId: 'm1', mediaUrl: 'https://i.redd.it/m1'})
  r.submitPost = async (opts: Record<string, unknown>) => {
    if (submitFail) throw Error('boom; internal detail')
    submitted.push(opts)
    return {id: 't3_new', url: 'https://reddit.com/x', permalink: '/r/x'}
  }
  r.submitComment = async () => ({id: 't1_new', permalink: '/r/x/c'})
  r.getComments = () => ({
    all: async () => [
      {
        id: 't1_a',
        parentId: 't3_new',
        authorName: 'someone',
        body: 'hi',
        createdAt: new Date(0),
        score: 2,
      },
    ],
  })

  server = createServer(async (req, rsp) => {
    await runWithContext({appName: 'soljet-postiz-intg'} as Context, () =>
      onReq(req, rsp),
    )
  })
  await new Promise<void>(resolve => {
    server.listen(0, '127.0.0.1', () => resolve())
  })
  const info = server.address() as AddressInfo
  serverURL = `http://127.0.0.1:${info.port}`
})

after(async () => {
  if (!server.listening) return
  await new Promise<void>((resolve, reject) => {
    server.close(err => (err ? reject(err) : resolve()))
  })
})

beforeEach(() => {
  submitted = []
  posterKey = key
  allowedSubs = 'test, r/Arboryx'
  submitFail = false
})

function call(endpoint: string, body: unknown, hdrKey = key) {
  return fetch(`${serverURL}/${endpoint}`, {
    body: JSON.stringify(body),
    headers: {'Content-Type': 'application/json', [posterKeyHeader]: hdrKey},
    method: 'POST',
  })
}

test('text post', async () => {
  const req: PostReq = {subreddit: 'r/test', title: 'T', body: 'hello'}
  const rsp = await call(Endpoint.Post, req)
  assert.equal(rsp.status, 200)
  assert.deepEqual<PostRsp>(await rsp.json(), {
    id: 't3_new',
    url: 'https://reddit.com/x',
    permalink: '/r/x',
  })
  assert.deepEqual(submitted, [
    {subredditName: 'test', title: 'T', text: 'hello'},
  ])
})

test('image post embeds the image as richtext', async () => {
  const req: PostReq = {
    subreddit: 'test',
    title: 'T',
    body: 'one\n\ntwo',
    imageUrl: 'https://storage.googleapis.com/x.png',
  }
  const rsp = await call(Endpoint.Post, req)
  assert.equal(rsp.status, 200)
  const [opts] = submitted
  assert.ok(opts?.richtext)
  assert.equal(opts?.text, undefined)
})

test('reply', async () => {
  const rsp = await call(Endpoint.Reply, {parentId: 't3_new', text: 'link'})
  assert.equal(rsp.status, 200)
  assert.deepEqual<ReplyRsp>(await rsp.json(), {
    id: 't1_new',
    permalink: '/r/x/c',
  })
})

test('comments', async () => {
  const rsp = await call(Endpoint.Comments, {postId: 't3_new'})
  assert.equal(rsp.status, 200)
  assert.deepEqual<CommentsRsp>(await rsp.json(), {
    comments: [
      {
        id: 't1_a',
        parentId: 't3_new',
        author: 'someone',
        body: 'hi',
        createdAt: new Date(0).toISOString(),
        score: 2,
      },
    ],
  })
})

test('bad poster key', async () => {
  const rsp = await call(Endpoint.Post, {subreddit: 's', title: 't'}, 'nope')
  assert.equal(rsp.status, 401)
  assert.equal(submitted.length, 0)
})

test('unset poster key fails closed', async () => {
  posterKey = undefined
  const rsp = await call(Endpoint.Post, {subreddit: 's', title: 't'})
  assert.equal(rsp.status, 503)
})

test('missing title', async () => {
  const rsp = await call(Endpoint.Post, {subreddit: 's'})
  assert.equal(rsp.status, 400)
})

test('404', async () => {
  const rsp = await fetch(serverURL)
  assert.equal(rsp.status, 404)
  assert.deepEqual<ErrorRsp>(await rsp.json(), {
    error: 'not found',
    status: 404,
  })
})

test('missing poster key header', async () => {
  const rsp = await fetch(`${serverURL}/${Endpoint.Post}`, {
    body: JSON.stringify({subreddit: 'test', title: 't'}),
    headers: {'Content-Type': 'application/json'},
    method: 'POST',
  })
  assert.equal(rsp.status, 401)
  assert.equal(submitted.length, 0)
})

test('array-valued poster key header', async () => {
  const req = {headers: {[posterKeyHeader]: [key, key]}}
  await assert.rejects(
    checkPosterKey(req as unknown as IncomingMessage),
    (err: Error & {status?: number}) => err.status === 401,
  )
})

test('unexpected errors return a generic 500 body', async () => {
  submitFail = true
  const rsp = await call(Endpoint.Post, {subreddit: 'test', title: 't'})
  assert.equal(rsp.status, 500)
  assert.deepEqual<ErrorRsp>(await rsp.json(), {
    error: 'server error',
    status: 500,
  })
})

test('allowed subreddit is normalized', async () => {
  const rsp = await call(Endpoint.Post, {subreddit: 'r/ARBORYX', title: 't'})
  assert.equal(rsp.status, 200)
  assert.equal(submitted[0]?.subredditName, 'arboryx')
})

test('disallowed subreddit', async () => {
  const rsp = await call(Endpoint.Post, {subreddit: 'r/other', title: 't'})
  assert.equal(rsp.status, 403)
  assert.equal(submitted.length, 0)
})

test('unset allowedSubreddits fails closed', async () => {
  for (const value of [undefined, '', ' , ']) {
    allowedSubs = value
    const rsp = await call(Endpoint.Post, {subreddit: 'test', title: 't'})
    assert.equal(rsp.status, 503)
    assert.deepEqual<ErrorRsp>(await rsp.json(), {
      error: 'allowedSubreddits setting is not configured',
      status: 503,
    })
  }
  assert.equal(submitted.length, 0)
})

test('oversized body', async () => {
  const rsp = await call(Endpoint.Post, {
    subreddit: 'test',
    title: 't',
    body: 'x'.repeat(maxBodyBytes + 1),
  })
  assert.equal(rsp.status, 413)
  assert.deepEqual<ErrorRsp>(await rsp.json(), {
    error: 'body too large',
    status: 413,
  })
  assert.equal(submitted.length, 0)
})
