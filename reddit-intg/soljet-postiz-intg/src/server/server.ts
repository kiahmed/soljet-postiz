import {once} from 'node:events'
import type {IncomingMessage, ServerResponse} from 'node:http'
import {RichTextBuilder} from '@devvit/shared-types/richtext/RichTextBuilder.js'
import {media, reddit, settings} from '@devvit/web/server'
import type {PartialJsonValue, T1, T3} from '@devvit/web/shared'
import {
  type CommentsReq,
  type CommentsRsp,
  Endpoint,
  EndpointMethod,
  type ErrorRsp,
  type PostReq,
  type PostRsp,
  posterKeyHeader,
  type ReplyReq,
  type ReplyRsp,
} from '../shared/api.ts'

type AnyRsp = PostRsp | ReplyRsp | CommentsRsp | ErrorRsp

class HttpError extends Error {
  readonly status: number
  constructor(status: number, msg: string) {
    super(msg)
    this.status = status
  }
}

export async function onReq(
  reqMsg: IncomingMessage,
  rspMsg: ServerResponse,
): Promise<void> {
  try {
    await route(reqMsg, rspMsg)
  } catch (err) {
    const status = err instanceof HttpError ? err.status : 500
    const msg =
      err instanceof HttpError
        ? err.message
        : `server error; ${err instanceof Error ? err.stack : err}`
    if (status === 500) console.error(msg)
    writeJson<ErrorRsp>(status, {error: msg, status}, rspMsg)
  }
}

async function route(
  reqMsg: IncomingMessage,
  rspMsg: ServerResponse,
): Promise<void> {
  // Strip the query string; managed tokens may arrive as ?externalToken=.
  const endpoint = reqMsg.url?.slice(1).split('?')[0] as Endpoint
  const method = EndpointMethod[endpoint]

  let rsp: AnyRsp
  if (method !== reqMsg.method) {
    rsp = {error: 'not found', status: 404}
  } else {
    await checkPosterKey(reqMsg)
    switch (endpoint) {
      case Endpoint.Post:
        rsp = await routePost(await readJson<PostReq>(reqMsg))
        break
      case Endpoint.Reply:
        rsp = await routeReply(await readJson<ReplyReq>(reqMsg))
        break
      case Endpoint.Comments:
        rsp = await routeComments(await readJson<CommentsReq>(reqMsg))
        break
      default:
        endpoint satisfies never
        rsp = {error: 'not found', status: 404}
        break
    }
  }

  writeJson<PartialJsonValue>('status' in rsp ? rsp.status : 200, rsp, rspMsg)
}

/** Reject callers without the shared `posterKey`; fail closed when unset. */
async function checkPosterKey(reqMsg: IncomingMessage): Promise<void> {
  const want = await settings.get<string>('posterKey')
  if (!want) throw new HttpError(503, 'posterKey setting is not configured')
  if (reqMsg.headers[posterKeyHeader] !== want)
    throw new HttpError(401, 'bad poster key')
}

export async function routePost(req: PostReq): Promise<PostRsp> {
  if (!req.subreddit || !req.title)
    throw new HttpError(400, 'subreddit and title are required')
  const common = {
    subredditName: req.subreddit.replace(/^r\//, ''),
    title: req.title,
    ...(req.flairId ? {flairId: req.flairId} : {}),
    ...(req.nsfw ? {nsfw: true} : {}),
  }

  let post: Awaited<ReturnType<typeof reddit.submitPost>>
  if (req.imageUrl) {
    const asset = await media.upload({url: req.imageUrl, type: 'image'})
    const richtext = new RichTextBuilder().image({
      mediaUrl: asset.mediaUrl,
      ...(req.imageCaption ? {caption: req.imageCaption} : {}),
    })
    for (const para of (req.body ?? '').split(/\n{2,}/)) {
      if (para.trim()) richtext.paragraph(p => p.text({text: para.trim()}))
    }
    post = await reddit.submitPost({...common, richtext})
  } else {
    post = await reddit.submitPost({...common, text: req.body ?? ''})
  }
  return {id: post.id, url: post.url, permalink: post.permalink}
}

export async function routeReply(req: ReplyReq): Promise<ReplyRsp> {
  if (!/^t[13]_/.test(req.parentId ?? '') || !req.text)
    throw new HttpError(400, 'parentId (t1_/t3_) and text are required')
  const comment = await reddit.submitComment({
    id: req.parentId as T1 | T3,
    text: req.text,
  })
  return {id: comment.id, permalink: comment.permalink}
}

export async function routeComments(req: CommentsReq): Promise<CommentsRsp> {
  if (!/^t3_/.test(req.postId ?? ''))
    throw new HttpError(400, 'postId (t3_) is required')
  const comments = await reddit
    .getComments({
      postId: req.postId as T3,
      sort: 'new',
      limit: Math.min(req.limit ?? 100, 500),
    })
    .all()
  return {
    comments: comments.map(c => ({
      id: c.id,
      parentId: c.parentId,
      author: c.authorName,
      body: c.body,
      createdAt: c.createdAt.toISOString(),
      score: c.score,
    })),
  }
}

async function readJson<T>(reqMsg: IncomingMessage): Promise<T> {
  const chunks: Uint8Array[] = []
  reqMsg.on('data', chunk => chunks.push(chunk))
  await once(reqMsg, 'end')
  try {
    return JSON.parse(`${Buffer.concat(chunks)}`)
  } catch {
    throw new HttpError(400, 'body must be JSON')
  }
}

function writeJson<T extends PartialJsonValue>(
  status: number,
  json: Readonly<T>,
  rsp: ServerResponse,
): void {
  const body = JSON.stringify(json)
  const len = Buffer.byteLength(body)
  rsp.writeHead(status, {
    'Content-Length': len,
    'Content-Type': 'application/json',
  })
  rsp.end(body)
}
