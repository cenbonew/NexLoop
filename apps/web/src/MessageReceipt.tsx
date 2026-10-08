import {useQuery} from '@tanstack/react-query';
import {chatError,readServiceReceipt,type MessageServiceReceipt} from './chat-api';
function receiptText(value:MessageServiceReceipt):string{
  const receipt=value.receipt;
  if(!value.run)return '消息已接受，尚未确认进入处理队列。';
  if(!receipt)return '已进入处理队列，尚无服务意图回执。';
  if(receipt.state==='confirmed')return '服务已履行并确认（治理回执）；不表示客户问题已解决。';
  if(receipt.business_action_success)return '服务已履行（治理回执）；不表示客户问题已解决。';
  if(receipt.state==='unknown')return '执行结果待核对，请查询原意图，勿重复发送。';
  if(receipt.state==='observed_fulfilled')return 'Provider 已报告完成，治理履行确认尚未完成。';
  if(receipt.state==='dispatching')return '服务正在执行，尚未确认履行。';
  if(receipt.state==='failed')return '服务执行已失败，履行未完成。';
  return '服务意图已接受，尚未确认履行。';
}
export function MessageReceipt({messageId}:{messageId:string}){
  const query=useQuery({queryKey:['message-service-receipt',messageId],queryFn:()=>readServiceReceipt(messageId),enabled:false,retry:false});
  return <div className="message-receipt"><button type="button" disabled={query.isFetching} onClick={()=>void query.refetch()}>{query.isFetching?'正在核对…':'查询处理与交付回执'}</button>
    {query.isError?<p role="alert">{chatError(query.error)}</p>:query.data&&<p role="status">{receiptText(query.data)}</p>}
    {query.data?.receipt&&<small>回执编号：{query.data.receipt.receipt_id}</small>}
  </div>;
}
