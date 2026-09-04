export function ErrorNotice({ message, onRetry }: { message: string; onRetry?: () => void }) {
  return <div className="notice notice--error" role="alert"><span>{message}</span>{onRetry && <button type="button" onClick={onRetry}>Try again</button>}</div>;
}
