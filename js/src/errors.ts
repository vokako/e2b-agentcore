/**
 * Map AWS errors onto the official E2B error classes.
 *
 * Callers keep writing `if (err instanceof NotFoundError)` etc. and it works the same against
 * this provider, because we throw the e2b classes themselves rather than look-alikes.
 */
import {
  AuthenticationError, NotFoundError, RateLimitError, SandboxError, TimeoutError,
} from 'e2b'
import { awsCode } from './config.js'

/** An E2B feature AgentCore has no equivalent for. The message says why and what to use. */
export class NotSupportedError extends SandboxError {
  constructor(message: string) {
    super(message)
    this.name = 'NotSupportedError'
  }
}

/** Marker read by tools/api-coverage.ts. */
export const UNSUPPORTED = Symbol.for('e2b-agentcore.unsupported')

/** A method stub that throws NotSupportedError (sync throw wrapped in a rejected promise). */
export function notSupported(feature: string, reason: string) {
  const stub = (..._args: unknown[]): never => {
    throw new NotSupportedError(`${feature} is not supported on AgentCore: ${reason}`)
  }
  ;(stub as any)[UNSUPPORTED] = reason
  return stub
}

export function mapAwsError(err: unknown): unknown {
  if (err instanceof SandboxError) return err
  const code = awsCode(err)
  const msg = (err as Error)?.message ?? String(err)
  if (!code || !(err as any)?.$metadata) {
    if ((err as Error)?.name === 'TimeoutError') return new TimeoutError(msg)
    return err
  }
  let mapped: Error
  if (['ResourceNotFoundException', 'NotFoundException'].includes(code)) mapped = new NotFoundError(msg)
  else if (['ThrottlingException', 'ServiceQuotaExceededException', 'TooManyRequestsException'].includes(code))
    mapped = new RateLimitError(msg)
  else if (['AccessDeniedException', 'UnrecognizedClientException', 'ExpiredTokenException'].includes(code))
    mapped = new AuthenticationError(msg)
  else mapped = new SandboxError(`${code}: ${msg}`)
  ;(mapped as any).cause = err
  return mapped
}

/** Run `fn`, re-throwing AWS SDK errors as E2B errors (original kept as `.cause`). */
export async function awsErrors<T>(fn: () => Promise<T>): Promise<T> {
  try {
    return await fn()
  } catch (err) {
    throw mapAwsError(err)
  }
}
