import {useQuery} from '@tanstack/react-query';
import {ApiError} from './api';
import {chatError,readServiceReceipt,readScopeDenial,type MessageServiceReceipt} from './chat-api';
/** The progress of the customer's message in plain words (UX audit B12, owner 2026-10-28): received, in progress, done, not done.
 * No receipt, intent or provider vocabulary and no identifiers. Still never claims more than the governed receipt says: a provider's
 * report without the governed confirmation is "in progress", an unknown result asks not to resend (AT-033). */
export function receiptText(value:MessageServiceReceipt):string{
  const receipt=value.receipt;
  if(!value.run)return '已收到，等待处理。';
  if(!receipt)return '处理中。';
  if(receipt.state==='confirmed'||receipt.business_action_success)return '已办理完成。';
  if(receipt.state==='unknown')return '处理中，正在确认结果；请不要重复发送。';
  if(receipt.state==='observed_fulfilled'||receipt.state==='dispatching')return '处理中，正在确认结果。';
  if(receipt.state==='failed')return '暂未办成。如需帮助，请继续留言。';
  return '已受理，处理中。';
}
/** A failed progress read is not a failed message: the message is listed, so it was sent; say only that the progress is not readable
 * now (sign-in and permission answers keep their own wording). */
export function progressError(error:unknown):string{
  return error instanceof ApiError&&(error.status===401||error.status===403)?chatError(error):'暂时查不到处理进度，请稍后再看；你的消息已经发出。';
}
export function MessageReceipt({messageId}:{messageId:string}){
  const query=useQuery({queryKey:['message-service-receipt',messageId],queryFn:async()=>{const receipt=await readServiceReceipt(messageId);const denied=receipt.receipt?.business_action_success?null:await readScopeDenial(messageId);return {receipt,denial:denied?.denial??null};},enabled:false,retry:false});
  const scope=query.data?.denial?.scope;
  return <div className="message-receipt"><button type="button" className="btn-secondary" disabled={query.isFetching} onClick={()=>void query.refetch()}>{query.isFetching?'正在查看…':'查看处理进度'}</button>
    {query.isError?<p role="alert">{progressError(query.error)}</p>:query.data&&<p role="status">{query.data.denial?'这项请求超出了可提供的服务范围，没有为你办理。':receiptText(query.data.receipt)}</p>}
    {/* AT-010: what can be provided, said to the customer in the denial itself. */}
    {scope&&<p role="status">可以提供：{scope.deliverable}；价格 {scope.price_amount} {scope.currency}。{scope.limitations.length?scope.limitations.join('；')+'。':''}</p>}
  </div>;
}
