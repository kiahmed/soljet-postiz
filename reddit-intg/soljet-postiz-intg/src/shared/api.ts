// Contract between the Cloud Run reddit-poster (bin/reddit_poster.py) and this
// app's external endpoints. Every request carries X-Poster-Key, checked against
// the app's `posterKey` secret setting.

/** Generic error detail for all responses. */
export type ErrorRsp = {error: string; status: number}

/**
 * Submit a post. With `imageUrl` (must be publicly fetchable, eg a signed GCS
 * URL) the image is uploaded to Reddit and embedded above `body`; without it
 * this is a plain markdown text post.
 */
export type PostReq = {
  subreddit: string
  title: string
  body?: string
  imageUrl?: string
  imageCaption?: string
  flairId?: string
  nsfw?: boolean
}
export type PostRsp = {id: string; url: string; permalink: string}

/** Comment on a post (t3_) or reply to a comment (t1_), markdown text. */
export type ReplyReq = {parentId: string; text: string}
export type ReplyRsp = {id: string; permalink: string}

/** Recent comments on a post, newest first, so the poster can pick replies. */
export type CommentsReq = {postId: string; limit?: number}
export type CommentInfo = {
  id: string
  parentId: string
  author: string
  body: string
  createdAt: string
  score: number
}
export type CommentsRsp = {comments: CommentInfo[]}

export type Endpoint = (typeof Endpoint)[keyof typeof Endpoint]
export const Endpoint = {
  Post: 'external/post',
  Reply: 'external/reply',
  Comments: 'external/comments',
} as const

export const EndpointMethod = {
  [Endpoint.Post]: 'POST',
  [Endpoint.Reply]: 'POST',
  [Endpoint.Comments]: 'POST',
} as const satisfies {[endpoint: string]: 'GET' | 'POST'}

/** Header the poster sends; compared with the `posterKey` setting. */
export const posterKeyHeader = 'x-poster-key'
