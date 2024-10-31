/*
 * TencentBlueKing is pleased to support the open source community by making 蓝鲸智云-DB管理系统(BlueKing-BK-DBM) available.
 * Copyright (C) 2017-2023 THL A29 Limited, a Tencent company. All rights reserved.
 * Licensed under the MIT License (the "License"); you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at https://opensource.org/licenses/MIT
 * Unless required by applicable law or agreed to in writing, software distributed under the License is distributed on
 * an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the License for the
 * specific language governing permissions and limitations under the License.
 */

// Package handler TODO
package handler

import (
	"fmt"
	"net/http"
	"regexp"
	"strings"

	"github.com/gin-gonic/gin"
	"github.com/samber/lo"

	"dbm-services/common/go-pubpkg/cmutil"
	"dbm-services/common/go-pubpkg/logger"
	"dbm-services/mysql/db-simulation/app/config"
	"dbm-services/mysql/db-simulation/app/service"
	"dbm-services/mysql/db-simulation/model"
)

// BaseHandler base handler
type BaseHandler struct {
	RequestId string
}

// Prepare prepare request
func (b *BaseHandler) Prepare(r *gin.Context, schema interface{}) error {
	requestId := r.GetString("request_id")
	if cmutil.IsEmpty(requestId) {
		err := fmt.Errorf("get request id error ~")
		b.SendResponse(r, err, nil)
		return err
	}
	b.RequestId = requestId
	if err := r.ShouldBind(&schema); err != nil {
		logger.Error("ShouldBind Failed %s", err.Error())
		b.SendResponse(r, err, nil)
		return err
	}
	logger.Info("param is %v", schema)
	return nil
}

// SendResponse send response to client
func (b *BaseHandler) SendResponse(r *gin.Context, err error, data interface{}) {
	if err != nil {
		r.JSON(http.StatusOK, Response{
			Code:      1,
			Message:   err.Error(),
			Data:      data,
			RequestID: b.RequestId,
		})
		return
	}
	r.JSON(http.StatusOK, Response{
		Code:      0,
		Message:   "successfully",
		Data:      data,
		RequestID: b.RequestId,
	})
}

// Response response data define
type Response struct {
	Data      interface{} `json:"data"`
	RequestID string      `json:"request_id"`
	Message   string      `json:"msg"`
	Code      int         `json:"code"`
}

// CreateClusterParam 创建临时的spider的集群参数
type CreateClusterParam struct {
	Pwd           string `json:"pwd"`
	PodName       string `json:"podname"`
	SpiderVersion string `json:"spider_version"`
}

// CreateTmpSpiderPodCluster 创建临时的spider的集群,多用于测试，debug
func (b *BaseHandler) CreateTmpSpiderPodCluster(r *gin.Context) {
	var param CreateClusterParam
	if err := b.Prepare(r, param); err != nil {
		return
	}
	ps := service.NewDbPodSets()
	ps.BaseInfo = &service.MySQLPodBaseInfo{
		PodName: param.PodName,
		RootPwd: param.Pwd,
		Charset: "utf8mb4",
	}
	ps.DbImage = config.GAppConfig.Image.Tendb57Img
	ps.SpiderImage, ps.TdbCtlImage = getSpiderAndTdbctlImg(param.SpiderVersion, LatestVersion)
	if err := ps.CreateClusterPod(); err != nil {
		logger.Error(err.Error())
		return
	}
	b.SendResponse(r, nil, "ok")
}

func replaceUnderSource(str string) string {
	return strings.ReplaceAll(str, "_", "-")
}

// getImgFromMySQLVersion 根据版本获取模拟执行运行的镜像配置
func getImgFromMySQLVersion(version string) (img string, err error) {
	img, errx := model.GetImageName("mysql", version)
	if errx == nil {
		logger.Info("get image from db img config: %s", img)
		return img, nil
	}
	switch {
	case regexp.MustCompile("5.5").MatchString(version):
		return config.GAppConfig.Image.Tendb55Img, nil
	case regexp.MustCompile("5.6").MatchString(version):
		return config.GAppConfig.Image.Tendb56Img, nil
	case regexp.MustCompile("5.7").MatchString(version):
		return config.GAppConfig.Image.Tendb57Img, nil
	case regexp.MustCompile("8.0").MatchString(version):
		return config.GAppConfig.Image.Tendb80Img, nil
	default:
		return "", fmt.Errorf("not match any version")
	}
}

func getSpiderAndTdbctlImg(spiderVersion, tdbctlVersion string) (spiderImg, tdbctlImg string) {
	return getSpiderImg(spiderVersion), getTdbctlImg(tdbctlVersion)
}

const (
	// LatestVersion latest version
	LatestVersion = "latest"
)

func getSpiderImg(version string) (img string) {
	if lo.IsEmpty(version) {
		version = LatestVersion
	}
	img, errx := model.GetImageName("spider", version)
	if errx == nil {
		return img
	}
	return config.GAppConfig.Image.SpiderImg
}

func getTdbctlImg(version string) (img string) {
	if lo.IsEmpty(version) {
		version = LatestVersion
	}
	img, errx := model.GetImageName("tdbctl", version)
	if errx == nil {
		return img
	}
	return config.GAppConfig.Image.TdbCtlImg
}
