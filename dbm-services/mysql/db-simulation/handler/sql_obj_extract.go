package handler

import (
	"github.com/gin-gonic/gin"
)

// SqlObjExtractHandler 用于提取SQL对象
type SqlObjExtractHandler struct {
	BaseHandler
}

type SqlObjExtractResponse struct {
}

type EvaluateSqlObjItem struct {
	ChangeType string   `json:"change_type"`
	Database   string   `json:"database"`
	Tables     []string `json:"tables"`
}

// RegisterRouter 注册路由
func (h *SqlObjExtractHandler) RegisterRouter(engine *gin.Engine) {
	engine.POST("/sql_obj_extract", h.SqlObjExtract)
}

// SqlObjExtract 提取SQL对象
func (h *SqlObjExtractHandler) SqlObjExtract(c *gin.Context) {
	h.SendResponse(c, nil, nil)
}
