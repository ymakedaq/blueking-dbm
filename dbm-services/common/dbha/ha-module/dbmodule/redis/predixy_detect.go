package redis

import (
	"encoding/json"
	"fmt"
	"strings"

	"dbm-services/common/dbha/ha-module/client"
	"dbm-services/common/dbha/ha-module/config"
	"dbm-services/common/dbha/ha-module/constvar"
	"dbm-services/common/dbha/ha-module/log"
	"dbm-services/common/dbha/ha-module/util"
)

// PredixyDetectInstance predixy detect instance
type PredixyDetectInstance struct {
	RedisDetectBase
}

// Detection detect predixy instance
func (ins *PredixyDetectInstance) Detection() error {
	err := ins.DoPredixyDetection()
	if err == nil && ins.Status == constvar.DBCheckSuccess {
		log.Logger.Debugf("predixy check ok and return ok . %s#%d", ins.Ip, ins.Port)
		return nil
	}

	if err != nil && ins.Status == constvar.AUTHCheckFailed {
		log.Logger.Debugf("predixy check auth failed. %s#%d|%s:%s %+v",
			ins.Ip, ins.Port, ins.GetType(), ins.Pass, err)
		return err
	}

	sshErr := ins.CheckSSH()
	if sshErr != nil {
		if util.CheckSSHErrIsAuthFail(sshErr) {
			ins.Status = constvar.AUTHCheckFailed
			log.Logger.Errorf("Predixy check ssh auth failed.ip:%s,port:%d,app:%s,status:%s",
				ins.Ip, ins.Port, ins.App, ins.Status)
		} else {
			ins.Status = constvar.SSHCheckFailed
			log.Logger.Errorf("Predixy check ssh failed.ip:%s,port:%d,app:%s,status:%s",
				ins.Ip, ins.Port, ins.App, ins.Status)
		}
		return sshErr
	} else {
		log.Logger.Debugf("Predixy check ssh success. ip:%s, port:%d, app:%s",
			ins.Ip, ins.Port, ins.App)
		ins.Status = constvar.SSHCheckSuccess
		return nil
	}
}

// DoPredixyDetection do predixy detect
func (ins *PredixyDetectInstance) DoPredixyDetection() error {
	r := &client.RedisClient{}
	addr := fmt.Sprintf("%s:%d", ins.Ip, ins.Port)
	if ins.Pass == "" {
		ins.Pass = GetPassByClusterID(ins.GetClusterId(), string(ins.GetType()))
	}
	r.Init(addr, ins.Pass, ins.Timeout, 0)
	defer r.Close()

	rsp, err := r.Type("twemproxy_mon")
	if err != nil {
		predixyErr := fmt.Errorf("do predixy cmd err,err: %s,info;%s",
			err.Error(), ins.ShowDetectionInfo())
		if util.CheckRedisErrIsAuthFail(err) {
			ins.Status = constvar.AUTHCheckFailed
		} else {
			ins.Status = constvar.DBCheckFailed
		}
		return predixyErr
	}

	rspInfo, ok := rsp.(string)
	if !ok {
		predixyErr := fmt.Errorf("predixy info response type is not string")
		log.Logger.Errorf(predixyErr.Error())
		ins.Status = constvar.DBCheckFailed
		return predixyErr
	}

	if strings.Contains(rspInfo, "none") {
		ins.Status = constvar.DBCheckSuccess
		return nil
	} else {
		predixyErr := fmt.Errorf("predixy exec detection failed,rsp:%s,info:%s",
			rspInfo, ins.ShowDetectionInfo())
		log.Logger.Errorf(predixyErr.Error())
		ins.Status = constvar.DBCheckFailed
		return predixyErr
	}
}

// Serialization serialize predixy detect instance
func (ins *PredixyDetectInstance) Serialization() ([]byte, error) {
	response := RedisDetectResponse{
		BaseDetectDBResponse: ins.NewDBResponse(),
		Pass:                 ins.Pass,
	}

	resByte, err := json.Marshal(&response)
	if err != nil {
		log.Logger.Errorf("Predixy serialization failed. err:%s", err.Error())
		return []byte{}, err
	}
	return resByte, nil
}

// ShowDetectionInfo show detect instance information
func (ins *PredixyDetectInstance) ShowDetectionInfo() string {
	str := fmt.Sprintf("ip:%s, port:%d, status:%s, DBType:%s",
		ins.Ip, ins.Port, ins.Status, ins.DBType)
	return str
}

// NewPredixyDetectInstance create predixy detect ins,
//
//	used by FetchDBCallback
func NewPredixyDetectInstance(ins *RedisDetectInfoFromCmDB,
	conf *config.Config) *PredixyDetectInstance {
	return &PredixyDetectInstance{
		RedisDetectBase: *GetDetectBaseByInfo(ins, constvar.PredixyMetaType, conf),
	}
}

// NewPredixyDetectInstanceFromRsp create predixy detect ins,
//
//	used by gm/DeserializeCallback
func NewPredixyDetectInstanceFromRsp(ins *RedisDetectResponse,
	conf *config.Config) *PredixyDetectInstance {
	return &PredixyDetectInstance{
		RedisDetectBase: *GetDetectBaseByRsp(ins, constvar.PredixyMetaType, conf),
	}
}
