update tb_rp_detail
set rs_type = JSON_UNQUOTE(JSON_EXTRACT(rs_types, '$[0]'))
where JSON_EXTRACT(rs_types, '$[0]') is not null;
update tb_rp_detail
set dedicated_biz = JSON_UNQUOTE(JSON_EXTRACT(dedicated_bizs, '$[0]'))
where JSON_EXTRACT(dedicated_bizs, '$[0]') is not null;
-- tb_rp_detail add column dedicated_bizs, rs_types;
alter table tb_rp_detail
add `dedicated_biz` int(11) DEFAULT '0' COMMENT '专属业务'
after bk_biz_id;
alter table tb_rp_detail
add `rs_type` varchar(64) DEFAULT 'PUBLIC' COMMENT '资源专用组件类型'
after dedicated_biz;
--  tb_rp_detail_archive add column dedicated_bizs, rs_types;
alter table tb_rp_detail_archive
add `dedicated_biz` int(11) DEFAULT '0' COMMENT '专属业务'
after bk_biz_id;
alter table tb_rp_detail_archive
add `rs_type` varchar(64) DEFAULT 'PUBLIC' COMMENT '资源专用组件类型'
after dedicated_biz;
-- drop old column dedicated_bizs, rs_types;
alter table tb_rp_detail drop dedicated_bizs,
    drop rs_types;
alter table tb_rp_detail_archive drop dedicated_bizs,
    drop rs_types;