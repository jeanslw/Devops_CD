-- --------------------------------------------------------
-- 主机:                           127.0.0.1
-- 服务器版本:                        8.4.3 - MySQL Community Server - GPL
-- 服务器操作系统:                      Win64
-- HeidiSQL 版本:                  12.8.0.6908
-- --------------------------------------------------------

/*!40101 SET @OLD_CHARACTER_SET_CLIENT=@@CHARACTER_SET_CLIENT */;
/*!40101 SET NAMES utf8 */;
/*!50503 SET NAMES utf8mb4 */;
/*!40103 SET @OLD_TIME_ZONE=@@TIME_ZONE */;
/*!40103 SET TIME_ZONE='+00:00' */;
/*!40014 SET @OLD_FOREIGN_KEY_CHECKS=@@FOREIGN_KEY_CHECKS, FOREIGN_KEY_CHECKS=0 */;
/*!40101 SET @OLD_SQL_MODE=@@SQL_MODE, SQL_MODE='NO_AUTO_VALUE_ON_ZERO' */;
/*!40111 SET @OLD_SQL_NOTES=@@SQL_NOTES, SQL_NOTES=0 */;


-- 导出 devops_glue 的数据库结构
CREATE DATABASE IF NOT EXISTS `devops_glue` /*!40100 DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci */ /*!80016 DEFAULT ENCRYPTION='N' */;
USE `devops_glue`;

-- 导出  表 devops_glue.admin_users 结构
CREATE TABLE IF NOT EXISTS `admin_users` (
  `username` varchar(255) NOT NULL,
  `password_hash` text NOT NULL,
  `updated_at` text DEFAULT (now()),
  PRIMARY KEY (`username`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- 数据导出被取消选择。

-- 导出  表 devops_glue.bots 结构
CREATE TABLE IF NOT EXISTS `bots` (
  `id` int NOT NULL AUTO_INCREMENT,
  `name` varchar(255) DEFAULT NULL,
  `type` varchar(32) DEFAULT 'custom',
  `webhook_url` text NOT NULL,
  `created_at` text DEFAULT (now()),
  PRIMARY KEY (`id`),
  UNIQUE KEY `name` (`name`)
) ENGINE=InnoDB AUTO_INCREMENT=2 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- 数据导出被取消选择。

-- 导出  表 devops_glue.cache 结构
CREATE TABLE IF NOT EXISTS `cache` (
  `cache_key` varchar(255) NOT NULL,
  `value` mediumtext NOT NULL,
  `expires_at` int DEFAULT NULL,
  PRIMARY KEY (`cache_key`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- 数据导出被取消选择。

-- 导出  表 devops_glue.ci_job_git_map 结构
CREATE TABLE IF NOT EXISTS `ci_job_git_map` (
  `job_name` varchar(255) NOT NULL,
  `git_platform` text,
  `build_provider` varchar(255) DEFAULT 'jenkins',
  `git_remote` text,
  `project_id` int DEFAULT NULL,
  `web_url` text,
  `current_path` text,
  `harbor_repository` text,
  `api_version` text,
  `status` varchar(255) DEFAULT 'active',
  PRIMARY KEY (`job_name`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- 数据导出被取消选择。

-- 导出  表 devops_glue.ci_pipeline_tags 结构
CREATE TABLE IF NOT EXISTS `ci_pipeline_tags` (
  `project` varchar(255) NOT NULL,
  `pipeline_iid` int NOT NULL,
  `tag` varchar(255) NOT NULL,
  `harbor_repository` text,
  `created_at` text DEFAULT (now()),
  PRIMARY KEY (`project`,`pipeline_iid`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- 数据导出被取消选择。

-- 导出  表 devops_glue.ci_platform_versions 结构
CREATE TABLE IF NOT EXISTS `ci_platform_versions` (
  `platform` varchar(255) NOT NULL,
  `version` text NOT NULL,
  PRIMARY KEY (`platform`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- 数据导出被取消选择。

-- 导出  表 devops_glue.deploy_logs 结构
CREATE TABLE IF NOT EXISTS `deploy_logs` (
  `id` int NOT NULL AUTO_INCREMENT,
  `project` varchar(255) DEFAULT NULL,
  `tag` varchar(255) DEFAULT NULL,
  `image` varchar(512) DEFAULT NULL,
  `deploy_type` varchar(32) DEFAULT NULL,
  `target` varchar(255) DEFAULT NULL,
  `status` varchar(32) DEFAULT NULL,
  `output` text,
  `created_at` text DEFAULT (now()),
  PRIMARY KEY (`id`)
) ENGINE=InnoDB AUTO_INCREMENT=8 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- 数据导出被取消选择。

-- 导出  表 devops_glue.servers 结构
CREATE TABLE IF NOT EXISTS `servers` (
  `id` int NOT NULL AUTO_INCREMENT,
  `name` varchar(255) DEFAULT NULL,
  `host` varchar(255) DEFAULT NULL,
  `port` int DEFAULT '22',
  `user` varchar(64) DEFAULT 'root',
  `type` varchar(32) DEFAULT 'ssh',
  `password` varchar(255) DEFAULT '',
  `created_at` text DEFAULT (now()),
  `tags` varchar(255) DEFAULT '',
  PRIMARY KEY (`id`),
  UNIQUE KEY `name` (`name`)
) ENGINE=InnoDB AUTO_INCREMENT=5 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- 数据导出被取消选择。

/*!40103 SET TIME_ZONE=IFNULL(@OLD_TIME_ZONE, 'system') */;
/*!40101 SET SQL_MODE=IFNULL(@OLD_SQL_MODE, '') */;
/*!40014 SET FOREIGN_KEY_CHECKS=IFNULL(@OLD_FOREIGN_KEY_CHECKS, 1) */;
/*!40101 SET CHARACTER_SET_CLIENT=@OLD_CHARACTER_SET_CLIENT */;
/*!40111 SET SQL_NOTES=IFNULL(@OLD_SQL_NOTES, 1) */;
