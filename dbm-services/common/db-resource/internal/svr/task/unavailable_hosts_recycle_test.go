/*
 * TencentBlueKing is pleased to support the open source community by making 蓝鲸智云-DB管理系统(BlueKing-BK-DBM) available.
 * Copyright (C) 2017-2023 THL A29 Limited, a Tencent company. All rights reserved.
 * Licensed under the MIT License (the "License"); you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at https://opensource.org/licenses/MIT
 * Unless required by applicable law or agreed to in writing, software distributed under the License is distributed on
 * an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the License for the
 * specific language governing permissions and limitations under the License.
 */

package task

import (
	"errors"
	"strings"
	"testing"

	"dbm-services/common/db-resource/internal/model"
	"dbm-services/common/db-resource/internal/svr/dbmapi"
)

func sampleUnavailable() []model.TbRpDetail {
	return []model.TbRpDetail{
		{BkHostID: 2001, IP: "127.0.0.1", BkCloudID: 0, Status: model.Unavailable},
		{BkHostID: 2002, IP: "127.0.0.2", BkCloudID: 0, Status: model.Unavailable},
	}
}

func TestUnavailableHostRecycleNoHostsSkipsDelete(t *testing.T) {
	restoreHostCheckDeps(t)
	deleted := false
	listUnavailableMachinesFn = func() ([]model.TbRpDetail, error) {
		return nil, nil
	}
	resourceDeleteFn = func(hosts []dbmapi.ResourceDeleteHost, event, remark string) error {
		deleted = true
		return nil
	}

	if err := UnavailableHostRecycle(); err != nil {
		t.Fatalf("UnavailableHostRecycle() err=%v", err)
	}
	if deleted {
		t.Fatal("no unavailable hosts must not call resource_delete")
	}
}

func TestUnavailableHostRecycleDeletesToRecycle(t *testing.T) {
	restoreHostCheckDeps(t)
	var deletedEvent string
	var deletedRemark string
	var deletedHosts []dbmapi.ResourceDeleteHost

	listUnavailableMachinesFn = func() ([]model.TbRpDetail, error) {
		return sampleUnavailable(), nil
	}
	resourceDeleteFn = func(hosts []dbmapi.ResourceDeleteHost, event, remark string) error {
		deletedEvent = event
		deletedRemark = remark
		deletedHosts = hosts
		return nil
	}

	if err := UnavailableHostRecycle(); err != nil {
		t.Fatalf("UnavailableHostRecycle() err=%v", err)
	}
	if deletedEvent != dbmapi.EventToRecycle {
		t.Fatalf("event=%s", deletedEvent)
	}
	if !strings.Contains(deletedRemark, "ext3") {
		t.Fatalf("remark should mention ext3, got %s", deletedRemark)
	}
	if len(deletedHosts) != 2 {
		t.Fatalf("hosts=%+v", deletedHosts)
	}
}

func TestUnavailableHostRecycleDeleteFailReturnsError(t *testing.T) {
	restoreHostCheckDeps(t)
	listUnavailableMachinesFn = func() ([]model.TbRpDetail, error) {
		return sampleUnavailable(), nil
	}
	resourceDeleteFn = func(hosts []dbmapi.ResourceDeleteHost, event, remark string) error {
		return errors.New("delete failed")
	}

	if err := UnavailableHostRecycle(); err == nil {
		t.Fatal("expected delete error so hosts stay Unavailable for retry")
	}
}
