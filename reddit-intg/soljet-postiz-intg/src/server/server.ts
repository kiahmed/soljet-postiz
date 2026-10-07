import {createHash, timingSafeEqual} from 'node:crypto'
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
    if (err instanceof HttpError) {
      if (err.status === 413) {
        // Unread body left on the socket: close it once the reply is out.
        rspMsg.setHeader('Connection', 'close')
        rspMsg.once('finish', () => reqMsg.destroy())
      }
      writeJson<ErrorRsp>(
        err.status,
        {error: err.message, status: err.status},
        rspMsg,
      )
      return
    }
    // Log the detail, but never hand stack traces to the caller.
    console.error(`server error; ${err instanceof Error ? err.stack : err}`)
    writeJson<ErrorRsp>(500, {error: 'server error', status: 500}, rspMsg)
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
export async function checkPosterKey(reqMsg: IncomingMessage): Promise<void> {
  const want = await settings.get<string>('posterKey')
  if (!want) throw new HttpError(503, 'posterKey setting is not configured')
  const got = reqMsg.headers[posterKeyHeader]
  // Missing or repeated header fails; hash both sides so the constant-time
  // compare sees equal-length buffers whatever the caller sent.
  if (typeof got !== 'string' || !timingSafeEqual(sha256(got), sha256(want)))
    throw new HttpError(401, 'bad poster key')
}

function sha256(s: string): Buffer {
  return createHash('sha256').update(s).digest()
}

function normSubreddit(s: string): string {
  return s.trim().replace(/^r\//i, '').toLowerCase()
}

/** Only subs in the `allowedSubreddits` setting; fail closed when unset. */
async function checkSubreddit(subreddit: string): Promise<string> {
  const list = (await settings.get<string>('allowedSubreddits')) ?? ''
  const allowed = new Set(list.split(',').map(normSubreddit).filter(Boolean))
  if (!allowed.size)
    throw new HttpError(503, 'allowedSubreddits setting is not configured')
  const sub = normSubreddit(subreddit)
  if (!allowed.has(sub))
    throw new HttpError(403, `subreddit not allowed: ${sub}`)
  return sub
}

export async function routePost(req: PostReq): Promise<PostRsp> {
  if (!req.subreddit || !req.title)
    throw new HttpError(400, 'subreddit and title are required')
  const common = {
    subredditName: await checkSubreddit(req.subreddit),
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

export const maxBodyBytes = 1024 * 1024

async function readJson<T>(reqMsg: IncomingMessage): Promise<T> {
  const body = await new Promise<Buffer>((resolve, reject) => {
    const chunks: Buffer[] = []
    let size = 0
    const onData = (chunk: Buffer): void => {
      size += chunk.length
      if (size > maxBodyBytes) {
        // Stop accumulating; onReq answers 413 then destroys the request.
        reqMsg.off('data', onData)
        reqMsg.pause()
        reject(new HttpError(413, 'body too large'))
        return
      }
      chunks.push(chunk)
    }
    reqMsg.on('data', onData)
    reqMsg.once('end', () => resolve(Buffer.concat(chunks)))
    reqMsg.once('error', reject)
  })
  try {
    return JSON.parse(`${body}`)
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
