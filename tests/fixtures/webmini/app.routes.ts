import { UserListComponent } from './app.component';

export const routes: Routes = [
  { path: 'users', component: UserListComponent },
  { path: 'orders/:id', component: OrderComponent },
];
