import { Component, Inject, OnInit } from '@angular/core';
import { FormBuilder, FormControl, FormGroup } from '@angular/forms';
import { MAT_DIALOG_DATA, MatDialogRef } from '@angular/material';
import { from, Observable } from 'rxjs';
import { filter, flatMap, share, startWith, takeWhile, toArray } from 'rxjs/operators';
import { UserToken } from '../../models/usertoken';
import { RaidenService } from '../../services/raiden.service';

export class OpenDialogPayload {
    readonly ownAddress: string;

    constructor(ownAddress: string) {
        this.ownAddress = ownAddress;
    }
}

export interface OpenDialogResult {
    tokenAddress: string;
    partnerAddress: string;
    settleTimeout: number;
    balance: number;
}

@Component({
    selector: 'app-open-dialog',
    templateUrl: './open-dialog.component.html',
    styleUrls: ['./open-dialog.component.css']
})
export class OpenDialogComponent implements OnInit {

    public form: FormGroup;
    public token: FormControl;
    public partnerAddress: FormControl;
    public balance: FormControl;
    public settleTimeout: FormControl;

    public filteredOptions: Observable<UserToken[]>;
    private tokens: Observable<UserToken[]>;

    constructor(
        @Inject(MAT_DIALOG_DATA) public data: OpenDialogPayload,
        public dialogRef: MatDialogRef<OpenDialogComponent>,
        public raidenService: RaidenService,
        private fb: FormBuilder,
    ) { }

    ngOnInit() {
        const data = this.data;
        this.form = this.fb.group({
            partner_address: ['', (control) => control.value === data.ownAddress ? {ownAddress: true} : undefined],
            token: '',
            balance: [0, (control) => control.value > 0 ? undefined : {invalidAmount: true}],
            settle_timeout: [500, (control) => control.value > 0 ? undefined : {invalidAmount: true}]
        });

        this.token = this.form.get('token') as FormControl;
        this.partnerAddress = this.form.get('partner_address') as FormControl;
        this.balance = this.form.get('balance') as FormControl;
        this.settleTimeout = this.form.get('settle_timeout') as FormControl;

        this.tokens = this.raidenService.getTokens(true).pipe(
            flatMap((tokens: UserToken[]) => from(tokens)),
            filter((token: UserToken) => !!token.connected),
            toArray(),
            share()
        );

        this.filteredOptions = this.form.controls['token'].valueChanges.pipe(
            startWith(''),
            takeWhile(value => typeof value === 'string'),
            flatMap(value => this._filter(value))
        );
    }

    accept() {
        const value = this.form.value;
        const result: OpenDialogResult = {
            tokenAddress: value.token,
            partnerAddress: value.partner_address,
            settleTimeout: value.settle_timeout,
            balance: value.balance,
        };

        this.dialogRef.close(result);
    }

    private _filter(value: string): Observable<UserToken[]> {
        const keyword = value.toLowerCase();
        return this.tokens.pipe(
            flatMap((tokens: UserToken[]) => from(tokens)),
            filter((token: UserToken) => {
                const name = token.name.toLowerCase();
                const symbol = token.symbol.toLowerCase();
                const address = token.address.toLowerCase();
                return name.startsWith(keyword) || symbol.startsWith(keyword) || address.startsWith(keyword);
            }),
            toArray()
        );
    }
}
