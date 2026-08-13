import { Component, EventEmitter, Inject, Input, Output } from '@angular/core';

@Injectable()
export class UserService {
  load(): string[] { return []; }
}

@Component({
  selector: 'app-user-list',
  template: '<ul><li *ngFor="let u of users">{{ u }}</li></ul>',
})
export class UserListComponent {
  @Input() users: string[] = [];
  @Output() picked = new EventEmitter<string>();

  constructor(@Inject(CONFIG) private cfg: Config,
              readonly svc: UserService) {}

  ngOnInit(): void {
    this.users = this.svc.load();
  }
}
