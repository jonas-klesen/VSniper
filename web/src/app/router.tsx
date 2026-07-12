import { createBrowserRouter } from 'react-router-dom';

import { AppLayout } from './App';
import { AiModelsPage } from '../pages/AiModelsPage';
import { CandidatesPage } from '../pages/CandidatesPage';
import { CostsPage } from '../pages/CostsPage';
import { DashboardPage } from '../pages/DashboardPage';
import { MyTastePage } from '../pages/MyTastePage';
import { SearchesPage } from '../pages/SearchesPage';
import { SettingsPage } from '../pages/SettingsPage';
import { StoragePage } from '../pages/StoragePage';

export const router = createBrowserRouter([
  {
    path: '/',
    element: <AppLayout />,
    children: [
      { index: true, element: <DashboardPage /> },
      { path: 'my-taste', element: <MyTastePage /> },
      { path: 'searches', element: <SearchesPage /> },
      { path: 'candidates', element: <CandidatesPage /> },
      { path: 'costs', element: <CostsPage /> },
      { path: 'ai-models', element: <AiModelsPage /> },
      { path: 'settings', element: <SettingsPage /> },
      { path: 'storage', element: <StoragePage /> },
    ],
  },
]);
