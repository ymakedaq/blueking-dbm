/*
 * TencentBlueKing is pleased to support the open source community by making 蓝鲸智云-DB管理系统(BlueKing-BK-DBM) available.
 * Copyright (C) 2017-2023 THL A29 Limited, a Tencent company. All rights reserved.
 * Licensed under the MIT License (the "License"); you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at https://opensource.org/licenses/MIT
 * Unless required by applicable law or agreed to in writing, software distributed under the License is distributed on
 * an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the License for the
 * specific language governing permissions and limitations under the License.
 */

// Package router routers
package router

import (
	"net/http"

	"github.com/gin-gonic/gin"

	"dbm-services/common/go-pubpkg/logger"
	"dbm-services/mysql/db-simulation/app/service"
	"dbm-services/mysql/db-simulation/handler"
)

// RegisterRouter register routers
func RegisterRouter(engine *gin.Engine) {
	engine.Handle("GET", "/ping", func(context *gin.Context) {
		context.String(http.StatusOK, "pong")
	})
	engine.POST("/app/debug", TurnOnDebug)

	simulationHandler := handler.SimulationHandler{}
	// query simulation task status info
	t := engine.Group("/simulation")
	t.POST("/task/file", simulationHandler.QuerySimulationFileResult)
	t.POST("/task", simulationHandler.QueryTask)
	// mysql
	g := engine.Group("/mysql")
	g.POST("/simulation", simulationHandler.TendbSimulation)
	g.POST("/task", simulationHandler.QueryTask)
	// syntax
	syntaxHandler := handler.SyntaxHandler{}
	s := engine.Group("/syntax")
	s.POST("/check/file", syntaxHandler.SyntaxCheckFile)
	s.POST("/check/sql", syntaxHandler.SyntaxCheckSQL)
	s.POST("/upload/ddl/tbls", syntaxHandler.CreateAndUploadDDLTblListFile)
	// rule
	manageRuleHandler := handler.ManageRuleHandler{}
	r := engine.Group("/rule")
	r.POST("/manage", manageRuleHandler.ManageRule)
	r.GET("/getall", manageRuleHandler.GetAllRule)
	r.POST("/update", manageRuleHandler.UpdateRule)
	// spider
	sp := engine.Group("/spider")
	sp.POST("/simulation", simulationHandler.TendbClusterSimulation)
	sp.POST("/create", simulationHandler.CreateTmpSpiderPodCluster)
}

// TurnOnDebug turn on debug,not del simulation pod
func TurnOnDebug(r *gin.Context) {
	logger.Info("current delpod: %v", service.DelPod)
	service.DelPod = !service.DelPod
	r.JSON(0, map[string]interface{}{
		"delpod": service.DelPod,
	})
}
