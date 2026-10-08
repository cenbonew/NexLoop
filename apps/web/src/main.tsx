import {StrictMode} from 'react';import {createRoot} from 'react-dom/client';
import {QueryClient,QueryClientProvider} from '@tanstack/react-query';import {App} from './App';import './styles.css';
const client=new QueryClient({defaultOptions:{queries:{retry:false},mutations:{retry:false}}});
createRoot(document.getElementById('root')!).render(<StrictMode><QueryClientProvider client={client}><App/></QueryClientProvider></StrictMode>);
